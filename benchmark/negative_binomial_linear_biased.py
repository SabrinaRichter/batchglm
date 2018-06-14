import os
# import datetime
from typing import Tuple

import numpy as np
import xarray as xr
import yaml

import pkg_constants
from models.negative_binomial_linear_biased import Simulator
from models.negative_binomial_linear_biased.estimator import Estimator

import utils.stats as stat_utils


def init_benchmark(
        root_dir: str,
        sim: Simulator,
        batch_size,
        stop_at_step=5000,
        learning_rate=0.05,
        config_file="config.yml"
):
    os.makedirs(root_dir, exist_ok=True)

    config = {
        "sim_data": "sim_data.h5",
        "plot_dir": "plot_dir",
    }

    os.makedirs(os.path.join(root_dir, config["plot_dir"]), exist_ok=True)
    sim.save(os.path.join(root_dir, config["sim_data"]))

    config["benchmark_samples"] = {
        "minibatch_sgd": prepare_benchmark_sample(
            root_dir=root_dir,
            working_dir="minibatch_sgd",
            batch_size=batch_size,
            stop_at_step=stop_at_step,
            learning_rate=learning_rate,
        ),
        "gradient_descent": prepare_benchmark_sample(
            root_dir=root_dir,
            working_dir="gradient_descent",
            batch_size=sim.num_samples,
            stop_at_step=stop_at_step,
            learning_rate=learning_rate,
        )
    }

    config_file = os.path.join(root_dir, config_file)
    with open(config_file, mode="w") as f:
        yaml.dump(config, f, default_flow_style=False)


def prepare_benchmark_sample(
        root_dir: str,
        working_dir: str,
        batch_size: int,
        stop_at_step: int,
        learning_rate: float = 0.05,
        stop_below_loss_change: float = None,
        save_checkpoint_steps=25,
        save_summaries_steps=25,
        export_steps=25,
        **kwargs

):
    os.makedirs(os.path.join(root_dir, working_dir), exist_ok=True)

    sample_config = {
        "working_dir": working_dir,
        "learning_rate": learning_rate,
        "batch_size": batch_size,
    }

    init_args = {
        # "working_dir": working_dir,
        "stop_at_step": stop_at_step,
        "stop_below_loss_change": stop_below_loss_change,
        "save_checkpoint_steps": save_checkpoint_steps,
        "save_summaries_steps": save_summaries_steps,
        "export_steps": export_steps,
        "export": ["a", "b", "mu", "r", "loss"],
        **kwargs
    }

    sample_config["init_args"] = init_args

    return sample_config


def get_benchmark_samples(root_dir: str, config_file="config.yml"):
    config_file = os.path.join(root_dir, config_file)
    with open(config_file, mode="r") as f:
        config = yaml.load(f)
    return list(config["benchmark_samples"].keys())


def run_benchmark(root_dir: str, sample: str, config_file="config.yml"):
    config_file = os.path.join(root_dir, config_file)
    with open(config_file, mode="r") as f:
        config = yaml.load(f)

    sim_data_file = os.path.join(root_dir, config["sim_data"])

    sample_config = config["benchmark_samples"][sample]

    working_dir = os.path.join(root_dir, sample_config["working_dir"])
    batch_size = sample_config["batch_size"]
    learning_rate = sample_config["learning_rate"]

    init_args = sample_config["init_args"]
    init_args["working_dir"] = working_dir

    print("loading data...", end="", flush=True)
    sim = Simulator()
    sim.load(sim_data_file)
    print("\t[OK]")

    print("starting estimation of benchmark sample '%s'..." % sample)
    estimator = Estimator(sim.data, batch_size=batch_size)
    estimator.initialize(**init_args)
    estimator.train(learning_rate=learning_rate)
    print("estimation of benchmark sample '%s' ready" % sample)


def load_benchmark_dataset(root_dir: str, config_file="config.yml") -> Tuple[Simulator, xr.Dataset]:
    config_file = os.path.join(root_dir, config_file)
    with open(config_file, mode="r") as f:
        config = yaml.load(f)

    sim_data_file = os.path.join(root_dir, config["sim_data"])
    sim = Simulator()
    sim.load(sim_data_file)

    benchmark_samples = config["benchmark_samples"]
    benchmark_data = []
    for smpl, cfg in benchmark_samples.items():
        data = xr.open_mfdataset(
            os.path.join(root_dir, cfg["working_dir"], "estimation-*.h5"),
            engine=pkg_constants.XARRAY_NETCDF_ENGINE,
            concat_dim="step"
        )
        data = data.sortby("global_step")
        data.coords["benchmark"] = smpl
        benchmark_data.append(data)
    benchmark_data = xr.auto_combine(benchmark_data, concat_dim="benchmark", coords="all")

    return sim, benchmark_data


def plot_benchmark(root_dir: str, config_file="config.yml"):
    print("loading config...", end="", flush=True)
    config_file = os.path.join(root_dir, config_file)
    with open(config_file, mode="r") as f:
        config = yaml.load(f)
    print("\t[OK]")

    plot_dir = os.path.join(root_dir, config["plot_dir"])

    print("loading data...", end="", flush=True)
    sim, benchmark_data = load_benchmark_dataset(root_dir)
    benchmark_data.coords["time_elapsed"] = benchmark_data.time_elapsed.cumsum("step")
    print("\t[OK]")

    import plotnine as pn
    import matplotlib.pyplot as plt

    from dask.diagnostics import ProgressBar

    def plot_mapd(mapd_val, name_prefix):
        with ProgressBar():
            df = mapd_val.to_dataframe("mapd").reset_index()

        plot = (pn.ggplot(df)
                + pn.aes(x="time_elapsed", y="mapd", group="benchmark", color="benchmark")
                + pn.geom_line()
                + pn.geom_vline(xintercept=df.loc[[np.argmin(df.mapd)]].time_elapsed.values[0], color="black")
                + pn.geom_hline(yintercept=np.min(df.mapd), alpha=0.5)
                )
        plot.save(os.path.join(plot_dir, name_prefix + ".time.svg"), format="svg")

        plot = (pn.ggplot(df)
                + pn.aes(x="global_step", y="mapd", group="benchmark", color="benchmark")
                + pn.geom_line()
                + pn.geom_vline(xintercept=df.loc[[np.argmin(df.mapd)]].global_step.values[0], color="black")
                + pn.geom_hline(yintercept=np.min(df.mapd), alpha=0.5)
                )
        plot.save(os.path.join(plot_dir, name_prefix + ".step.svg"), format="svg")

    print("plotting...")
    mapd_val: xr.DataArray = stat_utils.rmsd(
        np.exp(xr.DataArray(sim.a[0], dims=("genes",))),
        np.exp(benchmark_data.a.isel(design_params=0)), axis=[0])
    plot_mapd(mapd_val, "real_mu")

    mapd_val: xr.DataArray = stat_utils.rmsd(
        np.exp(xr.DataArray(sim.b[0], dims=("genes",))),
        np.exp(benchmark_data.b.isel(design_params=0)), axis=[0])
    plot_mapd(mapd_val, "real_r")

    print("ready")


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--root_dir', help='root directory of the benchmark', required=True)
    subparsers = parser.add_subparsers(help='select an action')

    act_init = subparsers.add_parser('init', help='set up a benchmark')
    act_init.set_defaults(action='init')
    act_init.add_argument('--num_samples', help='number of samples to generate', type=int, default=4000)
    act_init.add_argument('--num_genes', help='number of genes to generate', type=int, default=500)
    act_init.add_argument('--batch_size', help='batch size to use for mini-batch SGD', type=int, default=500)
    act_init.add_argument('--learning_rate', help='learning rate to use for all optimizers', type=float, default=0.05)
    act_init.add_argument('--stop_at_step', help='number of steps to run', type=int, default=5000)

    act_run = subparsers.add_parser('run', help='run a benchmark')
    act_run.set_defaults(action='run')
    act_run.add_argument('--benchmark_sample', help='If specified, only this benchmark sample will be executed')

    act_print_samples = subparsers.add_parser('print_samples', help='print all benchmark samples')
    act_print_samples.set_defaults(action='print_samples')

    act_plot = subparsers.add_parser('plot', help='generate plots')
    act_plot.set_defaults(action='plot')

    args, unknown = parser.parse_known_args()

    root_dir = os.path.expanduser(args.root_dir)

    action = args.action
    if action == "init":
        sim = Simulator(num_samples=args.num_samples, num_genes=args.num_genes)
        sim.generate()

        init_benchmark(
            root_dir=root_dir,
            sim=sim,
            batch_size=args.batch_size,
            stop_at_step=args.stop_at_step,
            learning_rate=args.learning_rate,
        )
    elif action == "run":
        if args.benchmark_sample is not None:
            run_benchmark(root_dir, args.benchmark_sample)
        else:
            benchmark_samples = get_benchmark_samples(root_dir)
            for smpl in benchmark_samples:
                run_benchmark(root_dir, smpl)
    elif action == "print_samples":
        benchmark_samples = get_benchmark_samples(root_dir)
        for smpl in benchmark_samples:
            print(smpl)
    elif action == "plot":
        plot_benchmark(root_dir)


if __name__ == '__main__':
    main()
