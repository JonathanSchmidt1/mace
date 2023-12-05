from copy import deepcopy
from typing import Tuple
from functools import partial
import ase
import torch
from e3nn import o3
from mace import data, tools
from mace.modules.blocks import SphericalHarmonics
from mace.modules.models import MACE
import torch.utils.benchmark as benchmark_

from mace.tools import torch_geometric, torch_tools

from mace_ops.ops.invariant_message_passing import InvariantMessagePassingTP
from mace_ops.ops.linear import Linear
from mace_ops.ops.symmetric_contraction import SymmetricContraction as CUDAContraction


def build_parser():
    """
    Create a parser for the command line tool.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Optimize a MACE model for CUDA inference."
    )
    parser.add_argument("--model", type=str, help="Path to the MACE model.")
    parser.add_argument(
        "--output",
        type=str,
        default="optimized_model.pt",
        help="Path to the output file.",
    )
    parser.add_argument(
        "--dtype",
        type=str,
        default="float32",
        help="Default dtype of the model.",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Benchmark the optimized model.",
        default=False,
    )
    parser.add_argument(
        "--benchmark_file",
        type=str,
        default="",
        help="Path to the benchmark file.",
    )
    return parser


def optimize_cuda_mace(model: MACE) -> None:
    """
    Optimize the MACE model for CUDA inference.
    """
    for param in model.parameters():
        param.requires_grad = False
    dtype = get_model_dtype(model)
    n_layers = int(model.num_interactions)
    sh_irreps = o3.Irreps.spherical_harmonics(3)
    spherical_harmonics = SphericalHarmonics(
        sh_irreps=sh_irreps,
        normalize=True,
        normalization="component",
        backend="opt",
    )
    model.spherical_harmonics = spherical_harmonics
    num_elements = model.node_embedding.linear.irreps_in.num_irreps
    for i in range(n_layers):
        model.interactions[i].linear_up = linear_matmul(model.interactions[i].linear_up)
        model.interactions[i].linear = linear_to_cuda(model.interactions[i].linear)
        model.interactions[i].tp = InvariantMessagePassingTP()
        if "Residual" in model.interactions[i].__class__.__name__:
            model.interactions[i].forward = partial(
                invariant_residual_interaction_forward, model.interactions[i]
            )
        else:
            model.interactions[i].forward = partial(
                invariant_interaction_forward, model.interactions[i]
            )
        symm_contract = model.products[i].symmetric_contractions
        all_weights = {}
        for j in range(len(symm_contract.contractions)):
            all_weights[str(j)] = {}
            all_weights[str(j)][3] = (
                symm_contract.contractions[j].weights_max.detach().clone().type(dtype)
            )
            all_weights[str(j)][2] = (
                symm_contract.contractions[j].weights[0].detach().clone().type(dtype)
            )
            all_weights[str(j)][1] = (
                symm_contract.contractions[j].weights[1].detach().clone().type(dtype)
            )
        irreps_in = o3.Irreps(model.products[i].symmetric_contractions.irreps_in)
        coupling_irreps = o3.Irreps([irrep.ir for irrep in irreps_in])
        irreps_out = o3.Irreps(model.products[i].symmetric_contractions.irreps_out)
        symmetric_contractions = CUDAContraction(
            coupling_irreps,
            irreps_out,
            all_weights,
            nthreadX=32,
            nthreadY=4,
            nthreadZ=1,
            dtype=dtype,
        )
        model.products[i].symmetric_contractions = SymmetricContractionWrapper(
            symmetric_contractions
        )
        model.products[i].linear = linear_matmul(model.products[i].linear)
    return model


class SymmetricContractionWrapper(torch.nn.Module):
    def __init__(self, symmetric_contractions):
        super().__init__()
        self.symmetric_contractions = symmetric_contractions

    def forward(self, x, y):
        y = y.argmax(dim=-1).int()
        return self.symmetric_contractions(x, y).squeeze()


def get_model_dtype(model: torch.nn.Module) -> torch.dtype:
    """Get the dtype of the model"""
    model_dtype = next(model.parameters()).dtype
    return model_dtype


class linear_matmul(torch.nn.Module):
    def __init__(self, linear_e3nn):
        super().__init__()
        num_channels_in = linear_e3nn.__dict__["irreps_in"].num_irreps
        num_channels_out = linear_e3nn.__dict__["irreps_out"].num_irreps
        self.weights = (
            linear_e3nn.weight.data.reshape(num_channels_in, num_channels_out)
            / num_channels_in**0.5
        )

    def forward(self, x):
        return torch.matmul(x, self.weights)


def linear_to_cuda(linear):
    return Linear(
        linear.__dict__["irreps_in"],
        linear.__dict__["irreps_out"],
        linear.instructions,
        linear.weight,
    )


def invariant_residual_interaction_forward(
    self,
    node_attrs: torch.Tensor,
    node_feats: torch.Tensor,
    edge_attrs: torch.Tensor,
    edge_feats: torch.Tensor,
    edge_index: torch.Tensor,
) -> Tuple[torch.Tensor, torch.Tensor]:
    sender = edge_index[0].int()
    receiver = edge_index[1].int()
    num_nodes = node_feats.shape[0]
    sc = self.skip_tp(node_feats, node_attrs)
    node_feats = self.linear_up(node_feats)
    tp_weights = self.conv_tp_weights(edge_feats)
    first_occurences = self.tp.calculate_first_occurences(
        receiver, num_nodes, torch.Tensor([]).int()
    )
    message = self.tp.forward(
        node_feats,
        edge_attrs,
        tp_weights.view(tp_weights.shape[0], -1, node_feats.shape[-1]),
        sender,
        receiver,
        first_occurences,
    )
    message = self.linear(message) / self.avg_num_neighbors
    return (
        message,
        sc,
    )  # [n_nodes, channels, (lmax + 1)**2]


def invariant_interaction_forward(
    self,
    node_attrs: torch.Tensor,
    node_feats: torch.Tensor,
    edge_attrs: torch.Tensor,
    edge_feats: torch.Tensor,
    edge_index: torch.Tensor,
) -> Tuple[torch.Tensor, torch.Tensor]:
    sender = edge_index[0].int()
    receiver = edge_index[1].int()
    num_nodes = node_feats.shape[0]
    node_feats = self.linear_up(node_feats)
    tp_weights = self.conv_tp_weights(edge_feats)
    first_occurences = self.tp.calculate_first_occurences(
        receiver, num_nodes, torch.Tensor().int()
    )
    tp_weights = tp_weights.view(tp_weights.shape[0], -1, node_feats.shape[-1])
    message = self.tp.forward(
        node_feats,
        edge_attrs,
        tp_weights,
        sender,
        receiver,
        first_occurences,
    )
    message = self.linear(message) / self.avg_num_neighbors
    # message = self.skip_tp(message, node_attrs)
    return (
        message,
        None,
    )  # [n_nodes, channels, (lmax + 1)**2]


def benchmark(model: MACE, benchmark_file: str, name: str) -> None:
    # Load data and prepare input
    try:
        atoms_list = ase.io.read(benchmark_file, format="extxyz", index=":")
    except Exception:
        print("Could not read file {}".format(benchmark_file))
        from ase import build

        # build very large diamond structure
        atoms = build.bulk("C", "diamond", a=3.567, cubic=True)
        atoms_list = [atoms.repeat((10, 10, 10))]
        print("Number of atoms", len(atoms_list[0]))

    configs = [data.config_from_atoms(atoms) for atoms in atoms_list]

    z_table = tools.AtomicNumberTable([int(z) for z in model.atomic_numbers])

    data_loader = torch_geometric.dataloader.DataLoader(
        dataset=[
            data.AtomicData.from_config(
                config, z_table=z_table, cutoff=model.r_max.item()
            )
            for config in configs
        ],
        batch_size=1,
        shuffle=False,
        drop_last=False,
    )
    batch = next(iter(data_loader)).to("cuda")
    print("num edges", batch.edge_index.shape)

    # Benchmark
    t0 = benchmark_.Timer(
        stmt="model(batch, training=False, compute_force=True)",
        globals={"model": model, "batch": batch},
        label=name,
    )
    print(t0.timeit(500))
    return None


def main(args=None):
    """
    Optimize a MACE model for CUDA inference.
    """
    parser = build_parser()
    args = parser.parse_args(args)
    torch_tools.set_default_dtype(args.dtype)
    torch_tools.init_device("cuda")
    model = torch.load(args.model).to("cuda")
    model_opt = optimize_cuda_mace(model)
    # get current folder of the model and append the args.output
    model_dir = "/".join(args.model.split("/")[:-1])
    model_opt_path = model_dir + "/" + args.output
    torch.save(model_opt, model_opt_path)
    if args.benchmark:
        # model_opt = torch.compile(model_opt)
        benchmark(model_opt, args.benchmark_file, "opt")
        model = torch.load(args.model).to("cuda")
        benchmark(model, args.benchmark_file, "orig")
    return None


if __name__ == "__main__":
    main()
