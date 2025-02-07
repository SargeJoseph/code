from time import sleep
import bittensor as bt
from datetime import datetime, timezone, timedelta

from coding.finetune import FinetunePipeline
from coding.protocol import StreamCodeSynapse
from coding.constants import COMPETITION_ID
from coding.utils.logging import log_event, clean_wandb
from coding.finetune.dockerutil import delete_all_containers

async def forward(self, synapse: StreamCodeSynapse):
    """
    The forward function is called by the validator every time step.

    It is responsible for querying the network and scoring the responses.

    Args:
        self (:obj:`bittensor.neuron.Neuron`): The neuron object which contains all the necessary state for the validator.

    """
    bt.logging.info("🚀 Starting forward loop...")
    if not FinetunePipeline.tasks_exist(self.config):
        FinetunePipeline.generate_tasks(self.config)
    
    if self.last_task_update + 3600 < self.block: # every half-day replace 50(half) the tasks
        FinetunePipeline.update_tasks(self.config, 50)
        self.last_task_update = self.block
    
    if not hasattr(self, 'finetune_eval_future'):
        delete_all_containers()
        sleep(10) # wait for containers to be truly deleted
        finetune_pipeline = FinetunePipeline(
            config=self.config,
        )
        self.finetune_eval_future = self.executor.submit(finetune_pipeline.evaluate)
    # Check if evaluation is complete
    if hasattr(self, "finetune_eval_future") and self.finetune_eval_future.done():
        self.finetune_results[COMPETITION_ID] = self.finetune_eval_future.result()
        delattr(self, "finetune_eval_future")  # Remove the future after getting results

    self.update_scores()

    log_event(
        self,
        {
            "step": self.step,
            **(
                self.finetune_results[COMPETITION_ID].__state_dict__()
                if COMPETITION_ID in self.finetune_results
                else {}
            ),
        },
    )

    # Call clean_wandb once every day
    if self.last_wandb_clean + 7200 < self.block:
        try:    
            clean_wandb(self)
            self.last_wandb_clean = self.block
        except Exception as e:
            bt.logging.error(f"Error cleaning wandb: {e}")

    sleep(60*5)