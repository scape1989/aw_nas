# -*- coding: utf-8 -*-
"""
Shared weights super net.
"""

from __future__ import print_function

import itertools
from collections import OrderedDict
import contextlib
import six

import torch

from aw_nas.common import assert_rollout_type, group_and_sort_by_to_node
from aw_nas.weights_manager.base import CandidateNet
from aw_nas.weights_manager.shared import SharedNet, SharedCell, SharedOp
from aw_nas.utils import data_parallel

__all__ = ["SubCandidateNet", "SuperNet"]


class SubCandidateNet(CandidateNet):
    """
    The candidate net for SuperNet weights manager.
    """

    def __init__(self, super_net, rollout, member_mask, gpus=tuple(), cache_named_members=False,
                 virtual_parameter_only=True):
        super(SubCandidateNet, self).__init__()
        self.super_net = super_net
        self._device = self.super_net.device
        self.gpus = gpus
        self.search_space = super_net.search_space
        self.member_mask = member_mask
        self.cache_named_members = cache_named_members
        self.virtual_parameter_only = virtual_parameter_only
        self._cached_np = None
        self._cached_nb = None

        self.genotypes = [g[1] for g in rollout.genotype_list()]
        self.genotypes_grouped = [group_and_sort_by_to_node(g[1]) for g in rollout.genotype_list() \
                                  if "concat" not in g[0]]

    @contextlib.contextmanager
    def begin_virtual(self):
        """
        On entering, store the current states (parameters/buffers) of the network
        On exiting, restore the stored states.
        Needed for surrogate steps of each candidate network,
        as different SubCandidateNet share the same set of SuperNet weights.
        """

        w_clone = {k: v.clone() for k, v in self.named_parameters()}
        if not self.virtual_parameter_only:
            buffer_clone = {k: v.clone() for k, v in self.named_buffers()}

        yield

        for n, v in self.named_parameters():
            v.data.copy_(w_clone[n])
        del w_clone

        if not self.virtual_parameter_only:
            for n, v in self.named_buffers():
                v.data.copy_(buffer_clone[n])
            del buffer_clone

    def get_device(self):
        return self._device

    def _forward(self, inputs):
        return self.super_net.forward(inputs, self.genotypes_grouped)

    def forward(self, inputs, single=False): #pylint: disable=arguments-differ
        if single or not self.gpus or len(self.gpus) == 1:
            return self._forward(inputs)
        # return data_parallel(self.super_net, (inputs, self.genotypes_grouped), self.gpus)
        return data_parallel(self, (inputs,), self.gpus, module_kwargs={"single": True})

    def forward_one_step(self, context, inputs=None):
        """
        Forward one step.
        Data parallism is not supported for now.
        """
        return self.super_net.forward_one_step(context, inputs, self.genotypes_grouped)

    def plot_arch(self):
        return self.super_net.search_space.plot_arch(self.genotypes)

    def named_parameters(self, prefix="", recurse=True): #pylint: disable=arguments-differ
        if self.member_mask:
            if self.cache_named_members:
                # use cached members
                if self._cached_np is None:
                    self._cached_np = []
                    for n, v in self.active_named_members(member="parameters", prefix=""):
                        self._cached_np.append((n, v))
                prefix = prefix + ("." if prefix else "")
                for n, v in self._cached_np:
                    yield prefix + n, v
            else:
                for n, v in self.active_named_members(member="parameters", prefix=prefix):
                    yield n, v
        else:
            for n, v in self.super_net.named_parameters(prefix=prefix):
                yield n, v

    def named_buffers(self, prefix="", recurse=True): #pylint: disable=arguments-differ
        if self.member_mask:
            if self.cache_named_members:
                if self._cached_nb is None:
                    self._cached_nb = []
                    for n, v in self.active_named_members(member="buffers", prefix=""):
                        self._cached_nb.append((n, v))
                prefix = prefix + ("." if prefix else "")
                for n, v in self._cached_nb:
                    yield prefix + n, v
            else:
                for n, v in self.active_named_members(member="buffers", prefix=prefix):
                    yield n, v
        else:
            for n, v in self.super_net.named_buffers(prefix=prefix):
                yield n, v

    def active_named_members(self, member, prefix="", recurse=True, check_visited=False):
        """
        Get the generator of name-member pairs active
        in this candidate network. Always recursive.
        """
        # memo, there are potential weight sharing, e.g. when `tie_weight` is True in rnn_super_net,
        # encoder/decoder share weights. If there is no memo, `sub_named_members` will return
        # 'decoder.weight' and 'encoder.weight', both refering to the same parameter, whereasooo
        # `named_parameters` (with memo) will only return 'encoder.weight'. For possible future
        # weight sharing, use memo to keep the consistency with the builtin `named_parameters`.
        memo = set()
        for n, v in self.super_net.sub_named_members(self.genotypes,
                                                     prefix=prefix,
                                                     member=member,
                                                     check_visited=check_visited):
            if v in memo:
                continue
            memo.add(v)
            yield n, v

    def state_dict(self, destination=None, prefix='', keep_vars=False):
        member_lst = []
        for n, v in itertools.chain(self.active_named_members(member="parameters", prefix=""),
                                    self.active_named_members(member="buffers", prefix="")):
            member_lst.append((n, v))
        state_dict = OrderedDict(member_lst)
        return state_dict

    def forward_one_step_callback(self, inputs, callback):
        # forward stem
        _, context = self.forward_one_step(context=None, inputs=inputs)
        callback(context.last_state, context)

        # forward the cells
        for _ in range(0, self.search_space.num_layers):
            num_steps = self.search_space.num_steps + self.search_space.num_init_nodes + 1
            for _ in range(num_steps):
                while True: # call `forward_one_step` until this step ends
                    _, context = self.forward_one_step(context)
                    callback(context.last_state, context)
                    if context.is_end_of_cell or context.is_end_of_step:
                        break
            # end of cell (every cell has the same number of num_steps)
        # final forward
        _, context = self.forward_one_step(context)
        callback(context.last_state, context)
        return context.last_state

class SuperNet(SharedNet):
    """
    A cell-based super network
    """
    NAME = "supernet"

    def __init__(self, search_space, device, rollout_type="discrete",
                 gpus=tuple(),
                 num_classes=10, init_channels=16, stem_multiplier=3,
                 max_grad_norm=5.0, dropout_rate=0.1,
                 use_stem="conv_bn_3x3", stem_stride=1, stem_affine=True,
                 cell_use_preprocess=True, cell_group_kwargs=None,
                 candidate_member_mask=True, candidate_cache_named_members=False,
                 candidate_virtual_parameter_only=False):
        """
        Args:
            candidate_member_mask (bool): If true, the candidate network's `named_parameters`
                or `named_buffers` method will only return parameters/buffers that is active,
                `begin_virtual` just need to store/restore these active variables.
                This should be more efficient.
            candidate_cache_named_members (bool): If true, the candidate network's
                named parameters/buffers will be cached on the first calculation.
                It should not cause any logical, however, due to my benchmark, this bring no
                performance increase. So default disable it.
            candidate_virtual_parameter_only (bool): If true, the candidate network's
                `begin_virtual` will only store/restore parameters, not buffers (e.g. running
                mean/running std in BN layer).
        """
        super(SuperNet, self).__init__(search_space, device, rollout_type,
                                       cell_cls=DiscreteSharedCell, op_cls=DiscreteSharedOp,
                                       gpus=gpus,
                                       num_classes=num_classes, init_channels=init_channels,
                                       stem_multiplier=stem_multiplier,
                                       max_grad_norm=max_grad_norm, dropout_rate=dropout_rate,
                                       use_stem=use_stem, stem_stride=stem_stride,
                                       stem_affine=stem_affine,
                                       cell_use_preprocess=cell_use_preprocess,
                                       cell_group_kwargs=cell_group_kwargs)

        # candidate net with/without parameter mask
        self.candidate_member_mask = candidate_member_mask
        self.candidate_cache_named_members = candidate_cache_named_members
        self.candidate_virtual_parameter_only = candidate_virtual_parameter_only

    def sub_named_members(self, genotypes,
                          prefix="", member="parameters", check_visited=False):
        prefix = prefix + ("." if prefix else "")

        # the common modules that will be forwarded by every candidate
        for mod_name, mod in six.iteritems(self._modules):
            if mod_name == "cells":
                continue
            _func = getattr(mod, "named_" + member)
            for n, v in _func(prefix=prefix+mod_name):
                yield n, v

        if check_visited:
            # only a subset of modules under `self.cells` will be forwarded
            # from the last output, parse the dependency backward
            visited = set()
            cell_idxes = [len(self.cells)-1]
            depend_nodes_lst = [{edge[1] for edge in genotype}.intersection(range(self._num_init))\
                                for genotype in genotypes]
            while cell_idxes:
                cell_idx = cell_idxes.pop()
                visited.update([cell_idx])
                # cg_idx is the cell group of the cell i
                cg_idx = self._cell_layout[cell_idx]
                depend_nodes = depend_nodes_lst[cg_idx]
                depend_cell_idxes = [cell_idx - self._num_init + node_idx
                                     for node_idx in depend_nodes]
                depend_cell_idxes = [i for i in depend_cell_idxes if i >= 0 and i not in visited]
                cell_idxes += depend_cell_idxes
        else:
            visited = list(range(self._num_layers))

        for cell_idx in sorted(visited):
            cell = self.cells[cell_idx]
            genotype = genotypes[self._cell_layout[cell_idx]]
            for n, v in cell.sub_named_members(genotype,
                                               prefix=prefix + "cells.{}".format(cell_idx),
                                               member=member,
                                               check_visited=check_visited):
                yield n, v

    # ---- APIs ----
    def assemble_candidate(self, rollout):
        return SubCandidateNet(self, rollout,
                               gpus=self.gpus,
                               member_mask=self.candidate_member_mask,
                               cache_named_members=self.candidate_cache_named_members,
                               virtual_parameter_only=self.candidate_virtual_parameter_only)

    @classmethod
    def supported_rollout_types(cls):
        return [assert_rollout_type("discrete")]

class DiscreteSharedCell(SharedCell):
    def num_out_channel(self):
        return self.num_out_channels * self.search_space.num_steps

    def forward(self, inputs, genotype_grouped): #pylint: disable=arguments-differ
        assert self._num_init == len(inputs)
        if self.use_preprocess:
            states = [op(_input) for op, _input in zip(self.preprocess_ops, inputs)]
        else:
            states = [s for s in inputs]

        for to_, connections in genotype_grouped:
            state_to_ = 0.
            for op_type, from_, _ in connections:
                out = self.edges[from_][to_](states[from_], op_type)
                state_to_ = state_to_ + out
            states.append(state_to_)

        return torch.cat(states[-self.search_space.num_steps:], dim=1)

    def forward_one_step(self, context, genotype_grouped):
        to_ = cur_step = context.next_step_index[1]
        if cur_step < self._num_init: # `self._num_init` preprocess steps
            ind = len(context.previous_cells) - (self._num_init - cur_step)
            ind = max(ind, 0)
            state = self.preprocess_ops[cur_step](context.previous_cells[ind])
            context.current_cell.append(state)
        elif cur_step < self._num_init + self._steps: # the following steps
            conns = genotype_grouped[cur_step - self._num_init][1]
            op_ind, current_op = context.next_op_index
            if op_ind == len(conns):
                # all connections added to context.previous_ops, sum them up
                state = sum([st for st in context.previous_op])
                context.current_cell.append(state)
                context.previous_op = []
            else:
                op_type, from_, _ = conns[op_ind]
                state, context = self.edges[from_][to_].forward_one_step(
                    context=context,
                    inputs=context.current_cell[from_] if current_op == 0 else None,
                    op_type=op_type)
        else: # final concat
            state = torch.cat(context.current_cell[-self.search_space.num_steps:], dim=1)
            context.current_cell = []
            context.previous_cells.append(state)
        return state, context

    def sub_named_members(self, genotype,
                          prefix="", member="parameters", check_visited=False):
        prefix = prefix + ("." if prefix else "")
        all_from = {edge[1] for edge in genotype}
        for i, pre_op in enumerate(self.preprocess_ops):
            if not check_visited or i in all_from:
                for n, v in getattr(pre_op, "named_" + member)\
                    (prefix=prefix+"preprocess_ops."+str(i)):
                    yield n, v

        for op_type, from_, to_ in genotype:
            edge_share_op = self.edges[from_][to_]
            for n, v in edge_share_op.sub_named_members(
                    op_type,
                    prefix=prefix + "edge_mod.f_{}_t_{}".format(from_, to_),
                    member=member):
                yield n, v

class DiscreteSharedOp(SharedOp):
    def forward(self, x, op_type): #pylint: disable=arguments-differ
        index = self.primitives.index(op_type)
        return self.p_ops[index](x)

    def forward_one_step(self, context, inputs, op_type): #pylint: disable=arguments-differ
        index = self.primitives.index(op_type)
        return self.p_ops[index].forward_one_step(context=context, inputs=inputs)

    def sub_named_members(self, op_type,
                          prefix="", member="parameters"):
        prefix = prefix + ("." if prefix else "")
        index = self.primitives.index(op_type)
        for n, v in getattr(self.p_ops[index], "named_" + member)(prefix="{}p_ops.{}"\
                                                .format(prefix, index)):
            yield n, v
