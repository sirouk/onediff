import re
import torch
import oneflow as flow
from typing import List, Dict, Any

from .log_utils import logger


def parse_device(args: List[Any], kwargs: Dict[str, Any]):
    if "device" in kwargs:
        return kwargs["device"]
    for x in args:
        if isinstance(x, (flow.device, torch.device)):
            return x
        if x in ["cpu", "cuda"]:
            return x
    return None


def check_device(current_device, target_device) -> bool:
    def _convert(device):
        assert isinstance(device, (str, torch.device, flow.device))
        if isinstance(device, torch.device):
            index = device.index if device.index is not None else 0
            return flow.device(device.type, index)
        if isinstance(device, str):
            return flow.device(device)
        return device

    return _convert(current_device) == _convert(target_device)

def get_constant_folding_info(deployable_module, torch_module: torch.nn.Module = None) -> Dict[str, flow.Tensor]:
    # convert str like 'variable_transpose_model.input_blocks.10.0.in_layers.2.weight_239'
    # to 'input_blocks.10.0.in_layers.2.weight'
    def convert_var_name(s: str, prefix="variable_transpose_"):
        s = re.sub(r"_[0-9]+$", "", s.removeprefix(prefix)).removeprefix("model.")
        return s

    from onediff.infer_compiler.deployable_module import DeployableModule
    if not isinstance(deployable_module, DeployableModule):
        raise TypeError(f"deployable_model must be a DeployableModule, got {type(deployable_module)}")
    if torch_module is None:
        torch_module = deployable_module._torch_module

    graph = deployable_module._deployable_module_dpl_graph
    if graph is None:
        raise RuntimeError(f"The graph of deployable_module is not built yet")

    result = {
        convert_var_name(k): v
        for k, v in zip(*graph._c_nn_graph.get_runtime_var_states())
        if k.startswith("variable_")
    }
    return result

def update_graph_with_constant_folding_info(module: torch.nn.Module, info: Dict[str, flow.Tensor]) -> None:
    from onediff.infer_compiler.deployable_module import DeployableModule
    if isinstance(module, DeployableModule):
        module = module._torch_module

    for k in info:
        orig_tensor = module.get_parameter(k)
        target_tensor = info.get(k, None)
        if target_tensor is None:
            raise RuntimeError(f"Can't find tensor named {k} in graph")
        target_tensor.copy_(
            flow.utils.tensor.from_torch(orig_tensor.permute(0, 2, 3, 1))
        )

# hooks for constant folding conv weights

STATE_UPDATED_ATTR = "_onediff_state_updated"
CONSTANT_FOLDING_INFO_ATTR = "_onediff_constant_folding_info"

def state_update_hook(module, incompatible_keys):
    if not hasattr(module, STATE_UPDATED_ATTR):
        return
    logger.info(f"load_state_dict called, set {STATE_UPDATED_ATTR} to True")
    setattr(module, STATE_UPDATED_ATTR, True)


def forward_generate_constant_folding_info_hook(module):
    if module._deployable_module_dpl_graph is None:
        return

    if getattr(module, CONSTANT_FOLDING_INFO_ATTR, None) is not None:
        return

    constant_folding_info = get_constant_folding_info(module)
    setattr(module, CONSTANT_FOLDING_INFO_ATTR, constant_folding_info)


def forward_pre_check_state_update_hook(module):
    if module._deployable_module_dpl_graph is None:
        return

    if not getattr(module._torch_module, STATE_UPDATED_ATTR, False):
        return

    constant_folding_info = getattr(module, CONSTANT_FOLDING_INFO_ATTR, None)
    if constant_folding_info is None:
        return

    update_graph_with_constant_folding_info(module, constant_folding_info)
    setattr(module._torch_module, STATE_UPDATED_ATTR, False)
