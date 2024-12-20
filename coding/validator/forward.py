# The MIT License (MIT)
# Copyright © 2024 Yuma Rao
# Copyright © 2023 Opentensor Foundation
# Copyright © 2024 Macrocosmos
# Copyright © 2024 Broke


# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import bittensor as bt
from datetime import datetime

from coding.utils.logging import log_event
from coding.finetune import FinetunePipeline
from coding.protocol import StreamCodeSynapse
from coding.rewards.codesim import CodeSimModel
from coding.constants import COMPETITION_END_DATE

async def forward(self, synapse: StreamCodeSynapse):
    """
    The forward function is called by the validator every time step.

    It is responsible for querying the network and scoring the responses.

    Args:
        self (:obj:`bittensor.neuron.Neuron`): The neuron object which contains all the necessary state for the validator.

    """
    bt.logging.info("🚀 Starting forward loop...")
    # TODO fix the competition end date checking , it is fixed in the main branch
    # check if the competition has ended and evaluation not started
    if datetime.now() > datetime.strptime(COMPETITION_END_DATE, "%Y-%m-%d"):
        if not self.finetune_results and not hasattr(self, 'finetune_eval_future'):
            self.finetune_result = None
            finetune_pipeline = FinetunePipeline(
                config=self.config,
                code_sim_model=CodeSimModel(code_scorer=self.code_scorer),
            )
            self.finetune_eval_future = self.executor.submit(finetune_pipeline.evaluate)
    # Check if evaluation is complete
    if hasattr(self, 'finetune_eval_future') and self.finetune_eval_future.done():
        self.finetune_results = self.finetune_eval_future.result()
        delattr(self, 'finetune_eval_future')  # Remove the future after getting results
    
    self.update_scores()

    log_event(
        self,
        {
            "step": self.step,
            **(self.finetune_results.__state_dict__() if hasattr(self, 'finetune_results') else {}),
        },
    )