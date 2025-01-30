import os
import pickle
import argparse
import traceback
import bittensor as bt
from typing import List
from pydantic import BaseModel
from .tracker import gather_all_logics
from concurrent.futures import ThreadPoolExecutor, as_completed

from .dockerutil import run_docker_container_from_base

from coding.schemas import Patch
from coding.schemas.context import Context
from coding.constants import COMPETITION_ID
from coding.rewards.codesim import CodeSimModel
from coding.schemas.tracking import TrackingInfo
from coding.constants import COMPETITION_ID, ALLOWED_MODULES, NUM_ALLOWED_CHARACTERS, ALLOWED_IMPORTS

from coding.tasks.swe import SWEBenchTask
from coding.datasets.swe import SWEBenchDataset
from coding.finetune.llm.manager import LLMManager
from coding.helpers.codeanal import verify_code_usage
from coding.utils.config import config as util_config
from coding.utils.config import add_validator_args



    
    
class FinetuneEventResults(BaseModel):
    trackers: List[TrackingInfo]
    competition_id: int = COMPETITION_ID
    
    def __state_dict__(self):
        return {
            "trackers": [tracker.model_dump() for tracker in self.trackers],
            "competition_id": COMPETITION_ID,
        }
    
    def public_state_dict(self):
        trackers = [tracker.model_dump() for tracker in self.trackers]
        for tracker in trackers:
            tracker["model"] = None
        return {
            "trackers": trackers,
            "competition_id": COMPETITION_ID,
        }



def generate_swe_tasks(ds: SWEBenchDataset, n: int = 1000, code_scorer =  None) -> List[SWEBenchTask]:
    tasks = []
    while len(tasks) < n:
        try:
            tasks.append(SWEBenchTask(llm=None, context=Context(**ds.get()), code_scorer=code_scorer))
        except Exception as e:
            bt.logging.error(f"Error generating task: {e}")
            print(traceback.format_exc())
    return tasks


def bittensor_injector(self):
    self.wallet = bt.wallet(config=self.config)
    self.dendrite = bt.dendrite(wallet=self.wallet)
    self.subtensor = bt.subtensor(config=self.config)
    self.metagraph = self.subtensor.metagraph(self.config.netuid)


def verify_logic(logic: dict) -> tuple[bool, str]:
    # Dictionary mapping modules to allowed functions/imports
    allowed_modules = ALLOWED_MODULES.copy()
    
    # Define allowed file extensions
    allowed_extensions = {'.yaml', '.py', '.txt', '.json'}
    
    for module in logic:
        # Handle folder paths by taking first component
        module_name = module.split("/")[0].split(".")[0]
        if module_name not in allowed_modules:
            allowed_modules.append(module_name)
            
    for key, value in logic.items():
        if value:
            # Check if the file extension is allowed
            file_extension = key.split('.')[-1]
            if f".{file_extension}" not in allowed_extensions:
                return False, f"File extension .{file_extension} is not allowed."
            
            # Create expanded allowed modules list that includes submodules and specific imports
            expanded_allowed = set()
            for mod in allowed_modules:
                expanded_allowed.add(mod)
                # If module is allowed, all its submodules are allowed
                for used_mod in value.split():
                    if used_mod.startswith(f"{mod}."):
                        expanded_allowed.add(used_mod)
                    # Check for specific allowed imports like "from os import getenv"
            usage_pass, usage_msg = verify_code_usage(value, list(expanded_allowed), ALLOWED_IMPORTS)
            if not usage_pass:
                return False, usage_msg
                
    total_chars = 0
    for key, value in logic.items():
        # Include full folder path in character count
        total_chars += len(key) + len(value)
        
    if total_chars > NUM_ALLOWED_CHARACTERS:
        return (
            False,
            f"Total characters: {total_chars} exceeds the limit of {NUM_ALLOWED_CHARACTERS}",
        )
        
    return True, "Logic is valid"

class FinetunePipeline:
    def __init__(
        self, config, tracking_logics: List[TrackingInfo] = None,
    ):
        self.config = config
        try:
            bittensor_injector(self)
        except Exception as e:
            bt.logging.error(f"Error injecting bittensor: {e}")
            print(traceback.format_exc())
        self.code_sim_model = CodeSimModel()
        self.trackers = []
        self.dataset = SWEBenchDataset()
        self.load_results()
        self.llm_manager = LLMManager()
        
        if tracking_logics is None:
            self.load_logics()
        else:
            self.tracking_logics = tracking_logics
        
        self.load_tasks()
        self.load_completed_trackers()
        # Register cleanup to be called when the object is deleted
        # self._finalizer = weakref.finalize(self, self.cleanup)

    def load_completed_trackers(self):
        if os.path.exists(f"{self.config.neuron.full_path}/completed_trackers_{COMPETITION_ID}.pkl"):
            with open(f"{self.config.neuron.full_path}/completed_trackers_{COMPETITION_ID}.pkl", "rb") as f:
                self.completed_trackers = pickle.load(f)
        else:
            self.completed_trackers = []
    
    def store_completed_trackers(self):
        with open(f"{self.config.neuron.full_path}/completed_trackers_{COMPETITION_ID}.pkl", "wb") as f:
            pickle.dump(self.completed_trackers, f)
    
    def load_tasks(self):
        if os.path.exists(f"{self.config.neuron.full_path}/tasks_{COMPETITION_ID}.pkl"):
            with open(f"{self.config.neuron.full_path}/tasks_{COMPETITION_ID}.pkl", "rb") as f:
                self.tasks = pickle.load(f)[:self.config.neuron.finetune_test_size]
                for task in self.tasks:
                    task.code_scorer = self.code_sim_model
        else:
            self.tasks = generate_swe_tasks(self.dataset, self.config.neuron.finetune_test_size, code_scorer=self.code_sim_model)
            self.store_tasks()

    def load_results(self):
        results_file = f"{self.config.neuron.full_path}/results_{COMPETITION_ID}.pkl"
        if os.path.exists(results_file):
            with open(results_file, "rb") as f:
                saved_results = pickle.load(f)
                self.trackers = saved_results.get("trackers", [])

    def store_logics(self):
        with open(f"{self.config.neuron.full_path}/logics_{COMPETITION_ID}.pkl", "wb") as f:
            pickle.dump(self.tracking_logics, f)
    
    def load_logics(self):
        if os.path.exists(f"{self.config.neuron.full_path}/logics_{COMPETITION_ID}.pkl"):
            with open(f"{self.config.neuron.full_path}/logics_{COMPETITION_ID}.pkl", "rb") as f:
                self.tracking_logics = pickle.load(f)
        else:
            self.tracking_logics = gather_all_logics(self)
            self.store_logics()
    
    @property
    def results(self) -> FinetuneEventResults:
        return FinetuneEventResults(
            trackers=self.trackers
        )

    # TODO add time taken and handle race condition due to parallel execution 
    # make use the same docker container for each task , where task repo files are copied over needs to change
    def evaluate(self) -> FinetuneEventResults:
        # gather all logics
        bt.logging.info("Gathering all logics...")
        bt.logging.info(f"Gathered {len(self.tracking_logics)} logics.")

        bt.logging.info("Verifying and building docker containers for each logic...")
        for tracker in self.tracking_logics:
            bt.logging.info(f"Verifying logic for hotkey {tracker.hotkey}...")
            pass_logic, pass_msg = verify_logic(tracker.logic)
            if not pass_logic:
                bt.logging.info(
                    f"Logic failed verification: {pass_msg} on tracker {tracker.hotkey}"
                )
                tracker.logic = {}
                continue
            bt.logging.info(f"Logic for hotkey {tracker.hotkey} passed verification.")

        bt.logging.info(f"Beginning evaluation of {len(self.tasks)} tasks...")
        for tracker_idx, tracking_logic in enumerate(self.tracking_logics):
            bt.logging.info(f"Processing tracker {tracker_idx + 1}/{len(self.tracking_logics)}")
            # Skip if no logic provided
            if not tracking_logic.logic:
                bt.logging.info(f"No logic provided for tracker {tracking_logic.hotkey}, skipping...")
                tracking_logic.score = 0
                self.trackers.append(tracking_logic)
                continue
            
            previous_tracker = next((tracker for tracker in self.trackers if str(tracker.logic) == str(tracking_logic.logic)), None)
            if previous_tracker is not None:
                bt.logging.info(f"Finetune: Using previously evaluated score for hotkey: {tracking_logic.hotkey}")
                tracking_logic.score = previous_tracker.score
                if tracking_logic.hotkey != previous_tracker.hotkey:
                    self.trackers.append(tracking_logic)
                continue

            # Otherwise, evaluate the logic
            bt.logging.info(f"Initializing LLM key for hotkey {tracking_logic.hotkey}...")
            self.llm_manager.init_key(tracking_logic.hotkey)
            bt.logging.info(f"Starting docker container for hotkey {tracking_logic.hotkey}...")
            scores = []
            # Create a thread pool to process tasks in parallel
            bt.logging.info("Starting thread pool for task processing...")
            with ThreadPoolExecutor() as executor:
                bt.logging.info("Thread pool started.")
                def process_task(task_data):
                    bt.logging.info(f"Processing task...")
                    task_idx, task = task_data
                    try:
                        bt.logging.info(f"Making request to container for hotkey {tracking_logic.hotkey}, task index {task_idx}...")
                        result = run_docker_container_from_base(
                            f"swe-logic-{str(tracking_logic.hotkey)}-{COMPETITION_ID}-{task_idx}".lower(),
                            task.repo,
                            tracking_logic.hotkey, 
                            task.query,
                            tracking_logic.logic
                        )
                        patch = Patch(**result)
                        bt.logging.info(f"Scoring response for hotkey {tracking_logic.hotkey}, task index {task_idx}...")
                        # TODO in the next comp uncomment the below
                        # score = task.score(patch, self.llm_manager.get_count())
                        score = task.score(patch, 1)
                        self.llm_manager.reset_count()
                        bt.logging.info(f"Score for hotkey {tracking_logic.hotkey}, task index {task_idx}: {score}")
                        return score
                    except Exception as e:
                        bt.logging.error(f"Request failed for hotkey {tracking_logic.hotkey}, task index {task_idx}: {e}")
                        print(traceback.format_exc())
                        return 0

                # Keep track of active futures and tasks
                active_futures = {}
                task_queue = list(enumerate(self.tasks))
                task_idx = 0

                # Start initial batch of 8 tasks
                bt.logging.info("Starting initial batch of 8 tasks...")
                while len(active_futures) < 8 and task_queue:
                    task_data = task_queue.pop(0)
                    future = executor.submit(process_task, task_data)
                    active_futures[future] = task_data
                
                bt.logging.info(f"Task queue drained, active futures left: {len(active_futures)}")
                # Process remaining tasks as others complete
                while active_futures:
                    completed_future = next(as_completed(active_futures))
                    task_data = active_futures.pop(completed_future)
                    
                    # Get score from completed task
                    score = completed_future.result()
                    scores.append(score)
                    bt.logging.info(f"Average score for hotkey {tracking_logic.hotkey}: {sum(scores) / len(scores)}")
                    
                    # Start next task if any remain
                    if task_queue:
                        task_data = task_queue.pop(0)
                        future = executor.submit(process_task, task_data)
                        active_futures[future] = task_data
                        
                    task_idx += 1
                    bt.logging.info(f"Completed task {task_idx}/{len(self.tasks)} for hotkey {tracking_logic.hotkey}")
            tracking_logic.score = sum(scores) / len(scores)
            self.trackers.append(tracking_logic)
            self.store_results()
            
            bt.logging.info(f"Cleaning up container for hotkey {tracking_logic.hotkey}...")
            bt.logging.info(f"Final score for hotkey {tracking_logic.hotkey}: {tracking_logic.score}")
            
        bt.logging.info("Evaluation complete!")
        self.store_results()
        self.verify_results()
        return self.results
    
    def __str__(self):
        return f"{self.__class__.__name__}(scores={self.scores!r}, models={self.tracking_logics!r})"

    def __repr__(self):
        return self.__str__()

    def __state_dict__(self):
        return {
            "scores": self.scores,
            "tracking_logics": [model.model_dump() for model in self.tracking_logics],
        }

    @staticmethod
    def start(
        config, code_sim_model: CodeSimModel = None
    ) -> FinetuneEventResults:
        if code_sim_model is None:
            code_sim_model = CodeSimModel()
        pipeline = FinetunePipeline(config, code_sim_model)
        result = pipeline.evaluate()
        pipeline.cleanup()  # Ensure cleanup is called after evaluation
        return result

    def store_tasks(self):
        with open(f"{self.config.neuron.full_path}/tasks_{COMPETITION_ID}.pkl", "wb") as f:
            for task in self.tasks:
                task.code_scorer = None
            pickle.dump(self.tasks, f)

    def store_results(self):
        results_file = f"{self.config.neuron.full_path}/results_{COMPETITION_ID}.pkl"
        temp_file = results_file + ".tmp"
        
        # Write to a temp file first
        with open(temp_file, "wb") as f:
            pickle.dump({"trackers": self.trackers}, f)
        
        # Replace the old file with the new
        os.replace(temp_file, results_file)

    @staticmethod
    def generate_tasks(config) -> List[SWEBenchTask]:
        dataset = SWEBenchDataset()
        code_scorer = CodeSimModel()
        tasks = generate_swe_tasks(dataset, config.neuron.finetune_test_size, code_scorer=code_scorer)
        with open(f"{config.neuron.full_path}/tasks_{COMPETITION_ID}.pkl", "wb") as f:
            for task in tasks:
                task.code_scorer = None
            pickle.dump(tasks, f)
    
    @staticmethod
    def tasks_exist(config):
        return os.path.exists(f"{config.neuron.full_path}/tasks_{COMPETITION_ID}.pkl")
    
    def verify_results(self):
        scores = []
        for tracker in self.trackers:
            scores.append(tracker.score)
        # if all the scores are 0 then we need to rerun the tasks
        self.trackers = []
        # delete the results file
        if all(score == 0 for score in scores):
            if os.path.exists(f"{self.config.neuron.full_path}/results_{COMPETITION_ID}.pkl"):
                os.remove(f"{self.config.neuron.full_path}/results_{COMPETITION_ID}.pkl")
            self.evaluate()
    
    def cleanup(self):
        """
        Delete the tasks file and any other task files
        """
        os.remove(f"{self.config.neuron.full_path}/tasks_{COMPETITION_ID}.pkl")
        # check if tasks_*.pkl exists and delete it if it does
        for file in os.listdir(self.config.neuron.full_path):
            if file.startswith("tasks_") and file.endswith(".pkl"):
                os.remove(os.path.join(self.config.neuron.full_path, file))
            if file.startswith("results_") and file.endswith(".pkl"):
                os.remove(os.path.join(self.config.neuron.full_path, file))
