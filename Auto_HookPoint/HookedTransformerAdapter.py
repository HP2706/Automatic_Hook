import logging
from transformer_lens.hook_points import HookedRootModule,  HookPoint
from transformer_lens.components import PosEmbed, Unembed
from transformer_lens.HookedTransformer import (
    HookedTransformer, 
    NON_HF_HOSTED_MODEL_NAMES
)
from transformers import AutoTokenizer, AutoModelForCausalLM, PreTrainedTokenizer, AutoConfig, PretrainedConfig
from .hook import auto_hook
from typing import Any, Iterable, Protocol, Sequence, Tuple, Union, List, Optional, Literal, cast, overload, Callable
from collections.abc import Sized
from jaxtyping import Float
import torch.nn as nn
import torch
from dataclasses import dataclass
from transformer_lens.HookedTransformerConfig import HookedTransformerConfig
import os


@dataclass
class HookedTransformerConfig_From_AutoConfig(HookedTransformerConfig):
    '''
    HookedTransformerConfig with a classmethod to create a HookedTransformerConfig 
    from a HuggingFace AutoConfig
    '''
    @classmethod
    def from_auto_config(
        cls, 
        auto_config: PretrainedConfig,
        **kwargs
    ) -> HookedTransformerConfig:
        return HookedTransformerConfig(
            n_layers=auto_config.num_hidden_layers,
            d_model=auto_config.hidden_size,
            n_ctx=auto_config.max_position_embeddings,
            d_head=auto_config.hidden_size // auto_config.num_attention_heads, #NOTE will this work for all models?
            **kwargs
        )

@dataclass
class HookedTransformerAdapterCfg:
    '''
    Defines the mapping between the input model 
    and the HookedTransformer special attributes
    '''
    block_attr: str
    lm_head_attr: str
    embedding_attr: str
    positional_embedding_attr: str
    last_layernorm_attr: Optional[str]

class AdaptedPosEmbed(PosEmbed):
    '''
    A class that adapts a regular nn.Embedding PosEmbed to a HookedTransformer PosEmbed
    '''
    @classmethod
    def from_regular_pos_embed(cls, pos_embed: nn.Embedding, cfg : HookedTransformerConfig):
        myclass = cls(cfg)
        myclass.W_pos = pos_embed.weight
        return myclass
    
class AdaptedUnembed(Unembed):
    '''
    A class that adapts a regular nn.Linear Unembed to a HookedTransformer Unembed
    '''
    @classmethod
    def from_regular_unembed(cls, unembed: nn.Linear, cfg : HookedTransformerConfig):
        myclass = cls(cfg)
        myclass.W_U = unembed.weight
        myclass.b_U = unembed.bias
        return myclass

class HookedTransformerAdapter(HookedTransformer):
    pos_embed : PosEmbed
    embed : nn.Embedding
    unembed : Unembed

    @overload
    def __init__(
        self,
        map_cfg: HookedTransformerAdapterCfg,
        hooked_transformer_cfg: HookedTransformerConfig,
        hf_model_name: str,
        *,
        move_to_device: bool = True,
        default_padding_side: Literal["left", "right"] = "right",
    ) -> None: ...

    @overload
    def __init__(
        self,
        map_cfg: HookedTransformerAdapterCfg,
        hooked_transformer_cfg: HookedTransformerConfig,
        *,
        model: nn.Module,
        tokenizer: Union[AutoTokenizer, PreTrainedTokenizer],
        move_to_device: bool = True,
        default_padding_side: Literal["left", "right"] = "right",
    ) -> None: ...

    def __init__(
        self,
        map_cfg: HookedTransformerAdapterCfg,
        hooked_transformer_cfg: HookedTransformerConfig,
        hf_model_name: Optional[str] = None,
        *,
        model: Optional[nn.Module] = None,
        tokenizer: Optional[Union[AutoTokenizer, PreTrainedTokenizer]] = None,
        move_to_device: bool = True,
        default_padding_side: Literal["left", "right"] = "right",
    ):
        """
        Initialize a HookedTransformerAdapter.

        This class adapts a HuggingFace transformer model to work with the
        transformer_lens.HookedTransformer.

        We achieve this by having an init function that assigns the same attributes as 
        transformer_lens.HookedTransformer.HookedTransformer.__init__() 
        the user has to provide a cfg object to map their model attributes to equivalent 
        HookedTransformer attributes.


        Args:
            map_cfg (HookedTransformerAdapterCfg): Configuration for adapter setup.
            hooked_transformer_cfg (HookedTransformerConfig): Configuration for HookedTransformer.
            hf_model_name (Optional[str]): Name of the Hugging Face model to load.
            model (Optional[nn.Module]): Pre-loaded Hugging Face model.
            tokenizer (Optional[Union[AutoTokenizer, PreTrainedTokenizer]]): Pre-loaded tokenizer.

        Raises:
            ValueError: If the input arguments are invalid.

        Note:
            Provide either hf_model_name or both model and tokenizer.
            map_cfg and hooked_transformer_cfg are always required.
        """
        #only initialize HookedRootModule not HookedTransformer.__init__
        HookedRootModule.__init__(self)
        self.validate_args(hf_model_name, model, tokenizer)

        self.map_cfg = map_cfg

        if isinstance(hf_model_name, str):
            self.model = auto_hook(AutoModelForCausalLM.from_pretrained(hf_model_name).to(hooked_transformer_cfg.device))
        elif isinstance(model, nn.Module) and tokenizer is not None:
            self.model = auto_hook(model.to(hooked_transformer_cfg.device))
        else:
            raise ValueError("Invalid input. Provide either a model name (str) or both model and tokenizer objects.")

        #this is needed for full transformer_lens compatibility
        self.cfg = HookedTransformerConfig.unwrap(hooked_transformer_cfg)

        if tokenizer is not None:
            self.set_tokenizer(tokenizer, default_padding_side=default_padding_side)
        elif self.cfg.tokenizer_name is not None:
            # If we have a tokenizer name, we can load it from HuggingFace
            if self.cfg.tokenizer_name in NON_HF_HOSTED_MODEL_NAMES:
                logging.warning(
                    "%s tokenizer not loaded. Please load manually.",
                    self.cfg.tokenizer_name,
                )
            else:
                # Hugging Face defaults to use_fast to True
                use_fast = True
                # Phi model's fast tokenizer does not support adding a BOS token, use_fast
                # should be False
                if "phi" in self.cfg.tokenizer_name.lower():
                    use_fast = False
                huggingface_token = os.environ.get("HF_TOKEN", None)
                self.set_tokenizer(
                    AutoTokenizer.from_pretrained(
                        self.cfg.tokenizer_name,
                        add_bos_token=True,
                        trust_remote_code=self.cfg.trust_remote_code,
                        use_fast=use_fast,
                        token=huggingface_token,
                    ),
                    default_padding_side=default_padding_side,
                )
        else:
            # If no tokenizer name is provided, we assume we're training on an algorithmic task and
            # will pass in tokens directly. In this case, we don't need a tokenizer.
            assert self.cfg.d_vocab != -1, "Must provide a tokenizer if d_vocab is not provided"
            self.tokenizer = None
            if default_padding_side != "right":
                logging.warning(
                    "default_padding_side is explictly given but ignored because tokenizer is not set."
                )

        self.tokenizer.pad_token = self.tokenizer.eos_token #type: ignore 
        self.device = hooked_transformer_cfg.device
        
        unembed = self._get_attr_recursively(self.model, map_cfg.lm_head_attr)
        self.unembed = AdaptedUnembed.from_regular_unembed(unembed, self.cfg)

        self.embed = self._get_attr_recursively(self.model, map_cfg.embedding_attr)
        self.hook_embed = HookPoint()  # [batch, pos, d_model]
        if map_cfg.last_layernorm_attr is not None:
            self.ln_final = self._get_attr_recursively(self.model, map_cfg.last_layernorm_attr)

        if self.cfg.positional_embedding_type != "rotary":
            regular_pos_embed = self._get_attr_recursively(self.model, map_cfg.positional_embedding_attr)
            self.pos_embed = AdaptedPosEmbed.from_regular_pos_embed(regular_pos_embed, self.cfg)
            self.hook_pos_embed = HookPoint()  # [batch, pos, d__dictmodel]

        if self.cfg.use_hook_tokens:
            self.hook_tokens = HookPoint()  # [batch, pos]

        self.blocks = self._get_model_blocks()
      
        self.setup()
        if move_to_device:
            assert self.device is not None, "device is not provided"
            self.to(self.device)

    def validate_args(
        self, 
        hf_model_name: Optional[str], 
        model: Optional[nn.Module], 
        tokenizer: Optional[Union[AutoTokenizer, PreTrainedTokenizer]]
    ):
        if (hf_model_name is not None) == (model is not None or tokenizer is not None):
            raise ValueError("Provide either a model name or both model and tokenizer objects, not both or neither.")

    def _get_model_blocks(self) -> Sequence[nn.Module]:
        var_name = self.map_cfg.block_attr
        if var_name is None:
            raise ValueError("block_attr is required when using start_at_layer and stop_at_layer")
        else:
            blocks = self._get_attr_recursively(self.model, var_name)
            assert isinstance(blocks, Iterable), f"Expected an iterable of modules, got {type(blocks)}"
            assert isinstance(blocks, Sized), f"Expected module to be have attribute __len__, got {type(blocks)}"
            
            wrapped_blocks = []
            for block in blocks:
                wrapped_blocks.append(self._wrap_block(block))
            
            return wrapped_blocks

    def _wrap_block(self, block: nn.Module) -> nn.Module:
        original_forward = block.forward
        def wrapped_forward(*args, **kwargs):
            '''
            Wraps the forward method of a block to ensure that the input is passed to the correct position
            '''
            return original_forward(*args)
        block.forward = wrapped_forward
        return block

    def _get_attr_recursively(self, obj, attr : str) -> Any:
        attrs = attr.split('.')
        for a in attrs:
            obj = getattr(obj, a)
        return obj

    def setup(self):
        '''
        Override HookedRootModule.setup 
        to avoid the _module wrapper in names
        '''
        self.hook_dict = self.model.hook_dict
        self.mod_dict = self.model.mod_dict
        for name, hook_point in self.hook_dict.items():
            hook_point.name = name

    @property
    def W_E(self) -> Float[torch.Tensor, "d_vocab d_model"]:
        """Convenience to get the embedding matrix."""
        return self.embed.weight

    @property
    def W_K(self) -> Float[torch.Tensor, "n_layers n_heads d_model d_head"]:
        raise NotImplementedError("This method is not implemented for HookedTransformerAdapter")

    @property
    def W_Q(self) -> Float[torch.Tensor, "n_layers n_heads d_model d_head"]:
        raise NotImplementedError("This method is not implemented for HookedTransformerAdapter")

    @property
    def W_V(self) -> Float[torch.Tensor, "n_layers n_heads d_model d_head"]:
        raise NotImplementedError("This method is not implemented for HookedTransformerAdapter")

    @property
    def W_O(self) -> Float[torch.Tensor, "n_layers n_heads d_head d_model"]:
        raise NotImplementedError("This method is not implemented for HookedTransformerAdapter")

    @property
    def W_in(self) -> Float[torch.Tensor, "n_layers d_model d_mlp"]:
        raise NotImplementedError("This method is not implemented for HookedTransformerAdapter")

    @property
    def W_gate(self) -> Union[Float[torch.Tensor, "n_layers d_model d_mlp"], None]:
        raise NotImplementedError("This method is not implemented for HookedTransformerAdapter")

    @property
    def W_out(self) -> Float[torch.Tensor, "n_layers d_mlp d_model"]:
        raise NotImplementedError("This method is not implemented for HookedTransformerAdapter")

    @property
    def b_K(self) -> Float[torch.Tensor, "n_layers n_heads d_head"]:
        raise NotImplementedError("This method is not implemented for HookedTransformerAdapter")

    @property
    def b_Q(self) -> Float[torch.Tensor, "n_layers n_heads d_head"]:
        raise NotImplementedError("This method is not implemented for HookedTransformerAdapter")

    @property
    def b_V(self) -> Float[torch.Tensor, "n_layers n_heads d_head"]:
        raise NotImplementedError("This method is not implemented for HookedTransformerAdapter")

    @property
    def b_O(self) -> Float[torch.Tensor, "n_layers d_model"]:
        raise NotImplementedError("This method is not implemented for HookedTransformerAdapter")

    @property
    def b_in(self) -> Float[torch.Tensor, "n_layers d_mlp"]:
        raise NotImplementedError("This method is not implemented for HookedTransformerAdapter")

    @property
    def b_out(self) -> Float[torch.Tensor, "n_layers d_model"]:
        raise NotImplementedError("This method is not implemented for HookedTransformerAdapter")

    @property
    def QK(self):
        raise NotImplementedError("This method is not implemented for HookedTransformerAdapter")

    @property
    def OV(self):
        raise NotImplementedError("This method is not implemented for HookedTransformerAdapter")
