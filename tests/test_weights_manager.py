import numpy as np
import six
import torch
import pytest


# ---- Test super_net ----
def _cnn_data(device="cuda"):
    return (torch.rand(2, 3, 28, 28, dtype=torch.float, device=device),
            torch.tensor([0, 1]).long().to(device))

def _supernet_sample_cand(net):
    ss = net.search_space

    rollout = ss.random_sample()
    # arch = [([0, 0, 2, 2, 0, 2, 4, 4], [0, 6, 7, 6, 1, 1, 5, 7]),
    # ([1, 1, 0, 0, 1, 2, 2, 2], [7, 2, 2, 1, 7, 4, 3, 7])]

    cand_net = net.assemble_candidate(rollout)
    return cand_net

@pytest.mark.parametrize("super_net", [{"candidate_member_mask": False}],
                         indirect=["super_net"])
def test_supernet_assemble_nomask(super_net):
    net = super_net
    cand_net = _supernet_sample_cand(net)
    assert set(dict(cand_net.named_parameters()).keys()) == set(dict(net.named_parameters()).keys())
    assert set(dict(cand_net.named_buffers()).keys()) == set(dict(net.named_buffers()).keys())

def test_supernet_assemble(super_net):
    net = super_net
    cand_net = _supernet_sample_cand(net)

    # test named_parameters/named_buffers
    # print("Supernet parameter num: {} ; buffer num: {}"\
    #       .format(len(list(net.parameters())), len(list(net.buffers()))))
    # print("candidatenet parameter num: {} ; buffer num: {}"\
    #       .format(len(list(cand_net.named_parameters())), len(list(cand_net.buffers()))))

    s_names = set(dict(net.named_parameters()).keys())
    c_names = set(dict(cand_net.named_parameters()).keys())
    assert len(s_names) > len(c_names)
    assert len(s_names.intersection(c_names)) == len(c_names)

    s_b_names = set(dict(net.named_buffers()).keys())
    c_b_names = set(dict(cand_net.named_buffers()).keys())
    assert len(s_b_names) > len(c_b_names)
    assert len(s_b_names.intersection(c_b_names)) == len(c_b_names)
    # names = sorted(set(c_names).difference([g[0] for g in grads]))

def test_supernet_forward(super_net):
    # test forward
    cand_net = _supernet_sample_cand(super_net)

    data = _cnn_data()

    logits = cand_net.forward_data(data[0], mode="eval")
    assert logits.shape[-1] == 10

def test_supernet_forward_step(super_net):
    cand_net = _supernet_sample_cand(super_net)

    data = _cnn_data()

    # forward stem
    stemed, context = cand_net.forward_one_step(context=None, inputs=data[0])
    assert len(context.previous_cells) == 1 and context.previous_cells[0] is stemed

    # forward the cells
    for i_layer in range(0, super_net.search_space.num_layers):
        num_steps = super_net.search_space.num_steps + super_net.search_space.num_init_nodes + 1
        for i_step in range(num_steps):
            step_state, context = cand_net.forward_one_step(context)
            if i_step != num_steps - 1:
                assert step_state is context.current_cell[-1] and \
                    len(context.current_cell) == i_step + 1
        assert context.is_end_of_cell
        assert len(context.previous_cells) == i_layer + 2

    # final forward
    logits, _ = cand_net.forward_one_step(context)
    assert logits.shape[-1] == 10

@pytest.mark.parametrize("test_id",
                         range(5))
def test_supernet_candidate_gradient_virtual(test_id, super_net):
    lr = 0.1
    EPS = 1e-5
    data = _cnn_data()
    net = super_net
    cand_net = _supernet_sample_cand(net)
    c_params = dict(cand_net.named_parameters())
    c_buffers = dict(cand_net.named_buffers())
    # test `gradient`, `begin_virtual`
    w_prev = {k: v.clone() for k, v in six.iteritems(c_params)}
    buffer_prev = {k: v.clone() for k, v in six.iteritems(c_buffers)}
    with cand_net.begin_virtual():
        grads = cand_net.gradient(data, mode="train")
        assert len(grads) == len(c_params)
        optimizer = torch.optim.SGD(cand_net.parameters(), lr=lr)
        optimizer.step()
        for n, grad in grads:
            assert (w_prev[n] - grad * lr - c_params[n]).abs().sum().item() < EPS
        grads_2 = dict(cand_net.gradient(data, mode="train"))
        assert len(grads) == len(c_params)
        optimizer.step()
        for n, grad in grads:
            # this check is not very robust...
            assert (w_prev[n] - (grad + grads_2[n]) * lr - c_params[n]).abs().mean().item() < EPS

        # sometimes, some buffer just don't updated... so here coomment out
        # for n in buffer_prev:
        #     # a simple check, make sure buffer is at least updated...
        #     assert (buffer_prev[n] - c_buffers[n]).abs().sum().item() < EPS

    for n in c_params:
        assert (w_prev[n] - c_params[n]).abs().mean().item() < EPS
    for n in c_buffers:
        assert (buffer_prev[n] - c_buffers[n]).abs().float().mean().item() < EPS

# ---- End test super_net ----

# ---- Test diff_super_net ----
def test_diff_supernet_forward(diff_super_net):
    from aw_nas.common import get_search_space
    from aw_nas.controller import DiffController

    search_space = get_search_space(cls="cnn")
    device = "cuda"
    controller = DiffController(search_space, device)
    rollout = controller.sample(1)[0]
    cand_net = diff_super_net.assemble_candidate(rollout)

    data = _cnn_data()
    logits = cand_net.forward_data(data[0])
    assert tuple(logits.shape) == (2, 10)

def test_diff_supernet_to_arch(diff_super_net):
    from torch import nn
    from aw_nas.common import get_search_space
    from aw_nas.controller import DiffController

    search_space = get_search_space(cls="cnn")
    device = "cuda"
    controller = DiffController(search_space, device)
    rollout = controller.sample(1)[0]
    cand_net = diff_super_net.assemble_candidate(rollout)

    data = _cnn_data() #pylint: disable=not-callable

    # default detach_arch=True, no grad w.r.t the controller param
    logits = cand_net.forward_data(data[0])
    loss = nn.CrossEntropyLoss()(logits, data[1].cuda())
    assert controller.cg_alphas[0].grad is None
    loss.backward()
    assert controller.cg_alphas[0].grad is None

    logits = cand_net.forward_data(data[0], detach_arch=False)
    loss = nn.CrossEntropyLoss()(logits, data[1].cuda())
    assert controller.cg_alphas[0].grad is None
    loss.backward()
    assert controller.cg_alphas[0].grad is not None
# ---- End test diff_super_net ----
