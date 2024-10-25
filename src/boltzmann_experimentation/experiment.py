import itertools
from typing import Iterator

import cyclopts
import matplotlib.pyplot as plt
import torch
from torch.utils.data import DataLoader
from tqdm import tqdm
from tqdm.auto import trange

import wandb
from boltzmann_experimentation.dataset import DatasetFactory
from boltzmann_experimentation.literals import GPU_NUMBER
from boltzmann_experimentation.logger import (
    add_file_logger,
    general_logger,
    init_wandb_run,
    metrics_logger,
)
from boltzmann_experimentation.loss import (
    ExactLoss,
)
from boltzmann_experimentation.miner import Miner
from boltzmann_experimentation.model import MODEL_TYPE, ModelFactory
from boltzmann_experimentation.settings import general_settings as g
from boltzmann_experimentation.settings import start_ts
from boltzmann_experimentation.validator import Validator
from boltzmann_experimentation.viz import (
    InteractivePlotter,
)

app = cyclopts.App()


@app.command
def run(
    model_type: MODEL_TYPE,
    num_miners: int = 5,
    num_communication_rounds: int = 3000,
    batch_size: int = 128,
    gpu_number: GPU_NUMBER | None = None,
):
    # change the device to f"cuda:{gpu_number}"
    g.num_miners = num_miners if num_miners else g.num_miners
    g.num_communication_rounds = (
        num_communication_rounds
        if num_communication_rounds
        else g.num_communication_rounds
    )
    g.batch_size = batch_size if batch_size else g.batch_size
    g.set_device(gpu_number)

    general_logger.info(f"Starting experiment on device {g.device}")

    PLOT_INTERACTIVELY = False
    SEED = 42

    # Generate the appropriate dataset for the model type
    train_dataset, val_dataset = DatasetFactory.create_dataset(model_type)
    general_logger.success(
        f"Created train dataset of length {len(train_dataset)} and val dataset of length {len(val_dataset)} for model {model_type}"
    )

    # Create DataLoader for training and validation sets
    train_loader = DataLoader(
        train_dataset,
        batch_size=g.batch_size,
        num_workers=g.num_workers_dataloader,
        shuffle=True,
    )
    val_loader = DataLoader(
        val_dataset,
        batch_size=g.batch_size,
        num_workers=g.num_workers_dataloader,
        shuffle=False,
    )

    def train_baselines(
        infinite_train_loader: Iterator[tuple[torch.Tensor, torch.Tensor]],
    ) -> None:
        for same_model_init in tqdm([True, False]):
            wandb.finish()
            run_name = (
                f"Central training: {'Same Init' if same_model_init else 'Diff Init'}"
            )
            init_wandb_run(run_name=run_name, model_type=model_type)
            if same_model_init:
                torch.manual_seed(SEED)
            model = ModelFactory.create_model(model_type)
            model.validate(val_loader)
            for _ in trange(g.num_communication_rounds):
                features, targets = next(infinite_train_loader)
                data = features.to(g.device), targets.to(g.device)
                model.train_step(data)
                model.validate(val_loader)

    # Infinite iterator for training
    infinite_train_loader = itertools.cycle(train_loader)
    validator_val_losses_compression_factor = {}

    # Train baselines
    train_baselines(infinite_train_loader)
    general_logger.success("Trained baselines")

    metrics_logger_id = None
    for same_model_init in tqdm([True, False]):
        for compression_idx, compression_factor in enumerate(tqdm([1, 10, 100, 1000])):
            # Set up logging of metrics
            wandb.finish()
            run_name = f"{'Same Init' if same_model_init else 'Diff Init'}: Compression {compression_factor}"
            init_wandb_run(run_name=run_name, model_type=model_type)
            log_dir = (
                g.results_dir
                / f"{start_ts}/logs/{same_model_init}:{compression_factor}"
            )
            if metrics_logger_id is not None:
                metrics_logger.remove(metrics_logger_id)
            add_file_logger(log_dir)

            general_logger.info(
                f"Running experiment with compression factor {compression_factor}"
            )
            g.compression_factor = compression_factor
            # Create torch models
            miner_models = []
            for _ in range(g.num_miners):
                if same_model_init:
                    torch.manual_seed(SEED)
                miner_models.append(ModelFactory.create_model(model_type))
            if same_model_init:
                torch.manual_seed(SEED)
            validator_model = ModelFactory.create_model(model_type)
            validator_model.torch_model.eval()
            general_logger.success(
                f"Created {len(miner_models)} miner models and a validator model"
            )

            # Create miners
            miners = [
                Miner(
                    model,
                    i,
                )
                for i, model in enumerate(miner_models)
            ]
            general_logger.success(f"Created {len(miners)} miners")

            # Choose the loss method dynamically using dependency injection
            loss_method = ExactLoss()
            general_logger.success(f"Created loss of type {type(loss_method)}")

            # Create validator
            validator = Validator(
                validator_model,
                g.compression_factor,
                loss_method,
            )
            general_logger.success("Created validator")

            # Set up interactive logging
            interactive_plotter = None
            if PLOT_INTERACTIVELY:
                xlim = (
                    val_dataset.features.min().item(),
                    val_dataset.features.max().item(),
                )
                ylim = (
                    val_dataset.targets.min().item() - 2,
                    val_dataset.targets.max().item() + 2,
                )
                interactive_plotter = InteractivePlotter(xlim, ylim)

            # Validate the initial model
            validator.model.validate(val_loader)

            # Training loop
            for round_num in trange(g.num_communication_rounds):
                validator.reset_slices_and_indices()
                for miner in miners:
                    features, targets = next(infinite_train_loader)
                    miner.data = features.to(g.device), targets.to(g.device)
                    miner.model.train_step(miner.data)
                    slice = miner.get_slice_from_indices(validator.slice_indices)
                    validator.add_miner_slice(slice)

                for miner in miners:
                    validator.calculate_miner_score(
                        validator.slices[miner.id],
                        miner.id,
                        miner.data,
                    )

                validator.model.torch_model = validator.model.aggregate_slices(
                    slices=list(validator.slices.values()),
                    slice_indices=validator.slice_indices,
                )
                validator.model.validate(val_loader)

                for miner in miners:
                    validator_model_params = torch.nn.utils.parameters_to_vector(
                        validator.model.torch_model.parameters()
                    )
                    validator_slice = validator_model_params[validator.slice_indices]
                    miner.model.update_with_slice(
                        validator_slice, validator.slice_indices
                    )

                if PLOT_INTERACTIVELY and interactive_plotter is not None:
                    interactive_plotter.plot_data_and_model(
                        torch_model=validator.model.torch_model,
                        features=val_dataset.features,
                        targets=val_dataset.targets,
                    )

            if PLOT_INTERACTIVELY:
                # Disable interactive mode when done and keep the final plot displayed
                plt.ioff()
                plt.show()

            validator_val_losses_compression_factor.update(
                {f"{same_model_init}/{compression_factor}": validator.model.val_losses}
            )

            # plot_scores(validator.scores)


if __name__ == "__main__":
    app()
