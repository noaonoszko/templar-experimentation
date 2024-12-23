from loguru import logger
import sys
from pathlib import Path
import wandb
from texp.config.settings import (
    general_settings as g,
)
from texp.schemas.training_components import TrainingComponents

# Remove the default logger
logger.remove()

# Set up the general logger (stdout)
general_logger = logger.bind(name="general")
general_logger.add(
    sys.stdout,
    level="INFO",
    filter=lambda record: record["extra"].get("name") == "general",
)

# Set up the metrics logger (file logging)
metrics_logger = logger.bind(name="metrics")


def add_file_logger(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    metrics_logger.add(
        log_dir / "metrics.log",
        level="INFO",
        rotation="10 MB",
        compression="zip",
        retention=5,
        filter=lambda record: record["extra"].get("name") == "metrics",
    )


def init_wandb_run(
    *,
    run_name: str,
    group: str,
    model_type: str,
    training_components: TrainingComponents,
) -> None:
    config = (
        {
            "model_type": model_type,
        }
        | g.__dict__
        | g.model_kwargs
        | training_components.__dict__
    )
    for param in g.wandb_legend_params:
        if param in config:
            run_name += f", {param}={None if config[param] == '' else config[param]}"
        else:
            general_logger.warning(
                f"Skipping wandb legend param '{param}' because it doesn't exist in the config that will be logged."
            )

    wandb.init(
        project="chakana",
        name=run_name,
        group=group,
        config=config,
        resume="allow",
    )
