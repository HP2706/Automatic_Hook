from __future__ import annotations
from inspect import isclass
import itertools
from torch import nn
from transformer_lens.hook_points import HookPoint, HookedRootModule
from typing import (
    TypeVar, 
    Generic, 
    Union, 
    Type, 
    Any, 
    Callable, 
    get_type_hints, 
    ParamSpec, 
    Optional, 
    Set, 
    TypeVar, 
    Type, 
    Union, 
    cast, 
    overload
)
from inspect import isclass
import functools
from torch.nn.modules.module import Module


T = TypeVar('T', bound=nn.Module)
P = ParamSpec('P')
R = TypeVar('R')


class HookedClass(Generic[T]):
    def __init__(self, module_class: Type[T]) -> T: # type: ignore
        self.module_class = module_class

    def __call__(self, *args: Any, **kwargs: Any) -> HookedInstance[T]:
        instance = self.module_class(*args, **kwargs)
        return auto_hook(instance)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.module_class, name)

    def unwrap_cls(self) -> Type[T]:
        '''recursively unwraps the module class'''
        for attr, value in self.module_class.__dict__.items():
            if isinstance(value, HookedClass):
                setattr(self.module_class, attr, value.unwrap_cls())
            elif isinstance(value, type) and issubclass(value, nn.Module):
                if isinstance(value, HookedClass):
                    setattr(self.module_class, attr, value.unwrap_cls())
                else:
                    Hooked_value = auto_hook(value)
                    if isinstance(Hooked_value, HookedClass):
                        setattr(self.module_class, attr, Hooked_value.unwrap_cls())
        return self.module_class

@overload
def auto_hook(module_or_class: Type[T]) -> HookedClass[T]: ...

@overload
def auto_hook(module_or_class: T) -> HookedInstance[T]: ...

def auto_hook(module_or_class: Union[T, Type[T]]) -> Union[HookedInstance[T], HookedClass[T]]:
    '''
    This function wraps either a module instance or a module class and returns a type that
    preserves the original module's interface plus an additional unwrap method.
    '''
    if isclass(module_or_class):
        return HookedClass(module_or_class)
    else:
        Hooked = HookedInstance(module_or_class) # type: ignore
        #NOTE we set the unwrap method to just return module_or_class
        Hooked.unwrap = lambda: module_or_class # type: ignore
        return cast(HookedInstance[T], Hooked)

class HookedInstance(HookedRootModule, Generic[T]):
    def __init__(self, module: T):
        super().__init__()
        # NOTE we need to name it in this way to not 
        # to avoid infinite regress and override

        
        self._module = module
        if not hasattr(self._module, 'hook_point'):
            self.hook_point = HookPoint()
        self._create_forward()
        self._wrap_submodules()
        self.setup()

    def __iter__(self):
        return iter(self._module)

    def new_attr_fn(self, name: str) -> Any:
        return getattr(self._module, name)

    #NOTE we override the nn.Module implementation to use _module only
    #NOTE this is not an ideal approach b
    def named_modules(self, memo: Set[Module] | None = None, prefix: str = '', remove_duplicate: bool = True):
        #NOTE BE VERY CAREFUL HERE
        
        if memo is None:
            memo = set()

        if self not in memo:
            memo.add(self)
            yield prefix, self
            #NOTE we dont need to iterate over the parameters here
            #because we already did that in the __init__
            for name, module in self._module.named_children():
                if module not in memo:
                    submodule_prefix = prefix + ('.' if prefix else '') + name
                    if isinstance(module, HookedInstance):
                        yield from module.named_modules(memo, submodule_prefix)
                    else:
                        yield submodule_prefix, module
                        if hasattr(module, 'named_modules'):
                            yield from module.named_modules(memo, submodule_prefix)

            if hasattr(self, 'hook_point'):
                hook_point_prefix = prefix + ('.' if prefix else '') + 'hook_point'
                yield hook_point_prefix, self.hook_point

    def unwrap_instance(self) -> T:
        for name, submodule in self._module.named_children():
            if isinstance(submodule, (nn.ModuleList, nn.Sequential)):
                unHooked_container = type(submodule)()
                for m in submodule:
                    unHooked_container.append(m.unwrap_instance() if isinstance(m, HookedInstance) else m)
                setattr(self._module, name, unHooked_container)
            elif isinstance(submodule, nn.ModuleDict):
                unHooked_container = type(submodule)()
                for key, m in submodule.items():
                    unHooked_container[key] = m.unwrap_instance() if isinstance(m, HookedInstance) else m
                setattr(self._module, name, unHooked_container)
            elif isinstance(submodule, HookedInstance):
                setattr(self._module, name, submodule.unwrap_instance())
        return self._module        

    def _wrap_submodules(self):
        
        """ for name, submodule in self._module.named_parameters():
            if isinstance(submodule, nn.Parameter):
                #NOTE IT IS NOT EASY TO WRAP A HOOKPOINT AROUND NN.PARAMETER
                #THIS IS CURRENTLY NOT SUPPORTED BUT WILL BE NEEDED TO SUPPORT
                #NOTE we can still set the hookpoint, but it doesnt provide meaningful utility 
                # as it is hard to wrap the output of NN.PARAMETER 
                
                #WE SET THE HOOKPOINT BUT IS NOT USED
                setattr(self._module, f'{name}.hook_point', HookPoint()) """ 
        
        for name, submodule in self._module.named_children():
            if isinstance(submodule, HookPoint):
                continue
             
            elif isinstance(submodule, (nn.ModuleList, nn.Sequential)):
                Hooked_container = type(submodule)() #initialize the container
                for i, m in enumerate(submodule):
                    Hooked_container.append(auto_hook(m))
                setattr(self._module, name, Hooked_container)
            elif isinstance(submodule, nn.ModuleDict):
                Hooked_container = type(submodule)()
                for key, m in submodule.items():
                    Hooked_container[key] = auto_hook(m)
                setattr(self._module, name, Hooked_container)
            else:
                setattr(self._module, name, auto_hook(submodule))

    def _create_forward(self):
        original_forward = self._module.forward
        original_type_hints = get_type_hints(original_forward)

        @functools.wraps(original_forward)
        def new_forward(*args: Any, **kwargs: Any) -> Any:
            return self.hook_point(original_forward(*args, **kwargs))

        new_forward.__annotations__ = original_type_hints
        self.forward = new_forward  # Assign to instance, not class

    def list_all_hooks(self):
        return [(hook, hook_point) for hook, hook_point in self.hook_dict.items()] 
    