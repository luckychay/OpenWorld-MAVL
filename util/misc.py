# Copyright (c) Aishwarya Kamath & Nicolas Carion. Licensed under the Apache License 2.0. All Rights Reserved
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
"""
Misc functions, including distributed helpers.

Mostly copy-paste from torchvision references.
"""
import os
import subprocess
import time
from collections import defaultdict, deque
import datetime
import pickle
from typing import Any, Dict, List, Optional

import torch
import torchvision
import torch.distributed as dist
from torch import Tensor


def get_sha():
    cwd = os.path.dirname(os.path.abspath(__file__))

    def _run(command):
        return subprocess.check_output(command, cwd=cwd).decode("ascii").strip()

    sha = "N/A"
    diff = "clean"
    branch = "N/A"
    try:
        sha = _run(["git", "rev-parse", "HEAD"])
        subprocess.check_output(["git", "diff"], cwd=cwd)
        diff = _run(["git", "diff-index", "HEAD"])
        diff = "has uncommited changes" if diff else "clean"
        branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    except Exception:
        pass
    message = f"sha: {sha}, status: {diff}, branch: {branch}"
    return message


def collate_fn(do_round, batch):
    batch = list(zip(*batch))
    final_batch = {}
    final_batch["samples"] = NestedTensor.from_tensor_list(batch[0], do_round)
    final_batch["targets"] = batch[1]
    if "positive_map" in batch[1][0]:
        # we batch the positive maps here
        # Since in general each batch element will have a different number of boxes,
        # we collapse a single batch dimension to avoid padding. This is sufficient for our purposes.
        max_len = max([v["positive_map"].shape[1] for v in batch[1]])
        nb_boxes = sum([v["positive_map"].shape[0] for v in batch[1]])
        batched_pos_map = torch.zeros((nb_boxes, max_len), dtype=torch.bool)
        cur_count = 0
        for v in batch[1]:
            cur_pos = v["positive_map"]
            batched_pos_map[cur_count: cur_count + len(cur_pos), : cur_pos.shape[1]] = cur_pos
            cur_count += len(cur_pos)

        assert cur_count == len(batched_pos_map)
        # assert batched_pos_map.sum().item() == sum([v["positive_map"].sum().item() for v in batch[1]])
        final_batch["positive_map"] = batched_pos_map.float()
    if "positive_map_eval" in batch[1][0]:
        # we batch the positive maps here
        # Since in general each batch element will have a different number of boxes,
        # we collapse a single batch dimension to avoid padding. This is sufficient for our purposes.
        max_len = max([v["positive_map_eval"].shape[1] for v in batch[1]])
        nb_boxes = sum([v["positive_map_eval"].shape[0] for v in batch[1]])
        batched_pos_map = torch.zeros((nb_boxes, max_len), dtype=torch.bool)
        cur_count = 0
        for v in batch[1]:
            cur_pos = v["positive_map_eval"]
            batched_pos_map[cur_count: cur_count + len(cur_pos), : cur_pos.shape[1]] = cur_pos
            cur_count += len(cur_pos)

        assert cur_count == len(batched_pos_map)
        # assert batched_pos_map.sum().item() == sum([v["positive_map"].sum().item() for v in batch[1]])
        final_batch["positive_map_eval"] = batched_pos_map.float()
    if "answer" in batch[1][0] or "answer_type" in batch[1][0]:
        answers = {}
        for f in batch[1][0].keys():
            if "answer" not in f:
                continue
            answers[f] = torch.stack([b[f] for b in batch[1]])
        final_batch["answers"] = answers

    return final_batch


class NestedTensor(object):
    def __init__(self, tensors, mask):
        self.tensors = tensors
        self.mask = mask

    def to(self, *args, **kwargs):
        cast_tensor = self.tensors.to(*args, **kwargs)
        cast_mask = self.mask.to(*args, **kwargs) if self.mask is not None else None
        return type(self)(cast_tensor, cast_mask)

    def decompose(self):
        return self.tensors, self.mask

    @classmethod
    def from_tensor_list(cls, tensor_list, do_round=False):
        # TODO make this more general
        if tensor_list[0].ndim == 3:
            # TODO make it support different-sized images
            max_size = tuple(max(s) for s in zip(*[img.shape for img in tensor_list]))
            # min_size = tuple(min(s) for s in zip(*[img.shape for img in tensor_list]))
            batch_shape = (len(tensor_list),) + max_size
            b, c, h, w = batch_shape
            if do_round:
                # Round to an even size to avoid rounding issues in fpn
                p = 128
                h = h if h % p == 0 else (h // p + 1) * p
                w = w if w % p == 0 else (w // p + 1) * p
                batch_shape = b, c, h, w

            dtype = tensor_list[0].dtype
            device = tensor_list[0].device
            tensor = torch.zeros(batch_shape, dtype=dtype, device=device)
            mask = torch.ones((b, h, w), dtype=torch.bool, device=device)
            for img, pad_img, m in zip(tensor_list, tensor, mask):
                pad_img[: img.shape[0], : img.shape[1], : img.shape[2]].copy_(img)
                m[: img.shape[1], : img.shape[2]] = False
        else:
            raise ValueError("not supported")
        return cls(tensor, mask)

    def __repr__(self):
        return repr(self.tensors)


def interpolate(input, size=None, scale_factor=None, mode="nearest", align_corners=None):
    # type: (Tensor, Optional[List[int]], Optional[float], str, Optional[bool]) -> Tensor
    """
    Equivalent to nn.functional.interpolate, but with support for empty channel sizes.
    """
    if input.numel() > 0:
        return torch.nn.functional.interpolate(input, size, scale_factor, mode, align_corners)

    assert input.shape[0] != 0 or input.shape[1] != 0, "At least one of the two first dimensions must be non zero"

    if input.shape[1] == 0:
        # Pytorch doesn't support null dimension on the channel dimension, so we transpose to fake a null batch dim
        return torch.nn.functional.interpolate(input.transpose(0, 1), size, scale_factor, mode,
                                               align_corners).transpose(0, 1)

    # empty batch dimension is now supported in pytorch
    return torch.nn.functional.interpolate(input, size, scale_factor, mode, align_corners)


def targets_to(targets: List[Dict[str, Any]], device):
    """Moves the target dicts to the given device."""
    excluded_keys = [
        "questionId",
        "tokens_positive",
        "tokens",
        "dataset_name",
        "sentence_id",
        "original_img_id",
        "nb_eval",
        "task_id",
        "original_id",
    ]
    return [{k: v.to(device) if k not in excluded_keys else v for k, v in t.items() if k != "caption"} for t in targets]


def inverse_sigmoid(x, eps=1e-5):
    x = x.clamp(min=0, max=1)
    x1 = x.clamp(min=eps)
    x2 = (1 - x).clamp(min=eps)
    return torch.log(x1 / x2)


def is_dist_avail_and_initialized():
    if not dist.is_available():
        return False
    if not dist.is_initialized():
        return False
    return True

def get_world_size():
    if not is_dist_avail_and_initialized():
        return 1
    return dist.get_world_size()

def get_rank():
    if not is_dist_avail_and_initialized():
        return 0
    return dist.get_rank()


def is_main_process():
    return get_rank() == 0


def all_gather(data):
    """
    Run all_gather on arbitrary picklable data (not necessarily tensors)
    Args:
        data: any picklable object
    Returns:
        list[data]: list of data gathered from each rank
    """
    world_size = get_world_size()
    if world_size == 1:
        return [data]

    # serialized to a Tensor
    buffer = pickle.dumps(data)
    storage = torch.ByteStorage.from_buffer(buffer)
    tensor = torch.ByteTensor(storage).to("cuda")

    # obtain Tensor size of each rank
    local_size = torch.tensor([tensor.numel()], device="cuda")
    size_list = [torch.tensor([0], device="cuda") for _ in range(world_size)]
    dist.all_gather(size_list, local_size)
    size_list = [int(size.item()) for size in size_list]
    max_size = max(size_list)

    # receiving Tensor from all ranks
    # we pad the tensor because torch all_gather does not support
    # gathering tensors of different shapes
    tensor_list = []
    for _ in size_list:
        tensor_list.append(torch.empty((max_size,), dtype=torch.uint8, device="cuda"))
    if local_size != max_size:
        padding = torch.empty(size=(max_size - local_size,), dtype=torch.uint8, device="cuda")
        tensor = torch.cat((tensor, padding), dim=0)
    dist.all_gather(tensor_list, tensor)

    data_list = []
    for size, tensor in zip(size_list, tensor_list):
        buffer = tensor.cpu().numpy().tobytes()[:size]
        data_list.append(pickle.loads(buffer))

    return data_list
