import warnings
from torch import nn
import inspect


def has_implemented_forward(module : nn.Module):
    '''
    this is for filtering torch modules by whether they have forward method
    note ModuleDict and ModuleList will have hasattr and callable 
    but you are not supposed to call forward on them.
    '''
    if not hasattr(module, "forward") or not callable(module.forward):
        return False
    
    try:
        source = inspect.getsource(module.forward)
        value = "raise NotImplementedError" not in source and "_forward_unimplemented" not in source
    except (OSError, TypeError):
        # If we can't get the source (e.g., built-in or C extension), assume it's implemented
        value = True
    
    if not value and not isinstance(module, (nn.ModuleList, nn.ModuleDict)):
        warnings.warn(
            f"Module {module} has forward method but"
            f"it is not implemented and is not a container module"
        )
        
    return value

def generate_expected_hookpoints(model : nn.Module, prefix=''):
    expected_hooks = []
    assert isinstance(model, nn.Module)
    for name, module in model.named_children():
        full_name = f"{prefix}.{name}" if prefix else name
        
        if not full_name.endswith('.hook_point') and has_implemented_forward(module):
            expected_hooks.append(f"{full_name}.hook_point")          

        if isinstance(module, (nn.ModuleList, nn.Sequential, nn.ModuleDict)):
            for key, child in (
                enumerate(module) 
                if isinstance(module, (nn.ModuleList, nn.Sequential)) 
                else module.items()
            ):
                print(f"child {child} with name : {key}")
                expected_hooks.extend(generate_expected_hookpoints(child, f"{full_name}.{key}"))

        expected_hooks.extend(generate_expected_hookpoints(module, full_name))
    
    return expected_hooks