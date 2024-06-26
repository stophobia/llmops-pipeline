import tempfile
import toml
import json
import asyncio
import numpy as np
from tqdm import tqdm
from string import Template
from typing import List, Dict
from datasets import (
    Dataset, DatasetDict, load_dataset
)

from ..gen.gemini import get_model as get_service_model
from ..gen.utils import call_service_llm

JSON_KEYS_TO_CHECK = {"contents"}

def _load_all_json_files(filenames):
    """
    _load_all_json_files loads up JSON files as Python dictionaries
    """
    all_json_dicts = []
    for filename in filenames:
        with open(filename) as f:
            all_json_dicts.append(json.load(f))
    return all_json_dicts

def _format_response(responses: List[Dict[str, str]]):
    """
    _format_response extracts information from the Python dictionaries that
    consists of synthetic data, and then reformat them as a standard message
    format ([{'role': ... 'content': ...}]).
    """
    final_instruction_answer_pairs = []

    for response in responses["contents"]:
        user_response_dict = {}
        assistant_response_dict = {}
        user_response_dict["content"] = response["instruction"]
        user_response_dict["role"] = "user"
        assistant_response_dict["content"] = response["response"]
        assistant_response_dict["role"] = "assistant"

        final_instruction_answer_pairs.append([user_response_dict, assistant_response_dict])

    seed_prompts = [responses["seed_prompt"]] * len(final_instruction_answer_pairs)

    return seed_prompts, final_instruction_answer_pairs

def _sampling(dataset_id, split, num_sample, seed):
    """
    _sampling samples a nubmer of data indices (as many as specified num_sample)
    from the given dataset_id[split]
    """
    np.random.seed(seed)

    ds = load_dataset(dataset_id, split=split)
    total_original_samples = len(ds)
    random_indices = np.random.randint(
        0, total_original_samples, size=(num_sample)
    )
    return ds.select(random_indices)

def _get_synth_data_gen_prompt_tmpl(prompt_tmpl_path):
    prompts = toml.load(prompt_tmpl_path)
    return prompts['synth_data_gen']['prompt']

def _craft_prompt(prompt_tmpl, instruction, response, topic):
    return Template(prompt_tmpl).substitute(
        instruction=instruction,
        response=response,
        topic=topic
    )

def _craft_prompts(samples, topic, prompt_tmpl_path):
    prompt_tmpl = _get_synth_data_gen_prompt_tmpl(prompt_tmpl_path)
    prompts = [
        _craft_prompt(
            prompt_tmpl,
            sample["messages"][0]["content"], 
            sample["messages"][1]["content"], 
            topic
        )
        for sample in samples
    ]
    return prompts

async def _gen_synth_data(prompts, model, eval_workers):
    """
    _gen_synth_data concurrently generates synthetic data based on the given prompts
    """
    generated_data = []
    for idx in tqdm(range(0, len(prompts), eval_workers), desc="batches"):
        partial_prompts = prompts[idx:idx+eval_workers]
        tasks = [
            asyncio.create_task(
                call_service_llm(model, eval_prompt, JSON_KEYS_TO_CHECK, retry_num=5, job_num=idx)
            ) for eval_prompt in partial_prompts
        ]
        results = await asyncio.gather(*tasks)
        results = sorted(results, key=lambda item: item[0])
        results = [result[1] for result in results]
        generated_data.extend(results)
        
    return generated_data

async def synth_data_generation(
    dataset_id, split, 
    seed, num_sample,
    topic, prompt_tmpl_path,
    service_model_name, gen_workers
):
    """
    synth_data_generation does the following jobs in order
    1. geting a number of samples from the original dataset
    2. making concret prompts for each sample that are used to 
    generate synthetic dataset by service LLM
    3. concurrently generating synthetic data 
    4. exporting generated results to external JSON files
    """
    samples = _sampling(dataset_id, split, num_sample, seed)
    prompts = _craft_prompts(samples, topic, prompt_tmpl_path)

    gen_model = get_service_model(service_model_name)
    print("Generating synthetic data")
    generated_data = await _gen_synth_data(prompts, gen_model, gen_workers)

    save_dir_path = tempfile.gettempdir()
    filenames = []
    print("Exporting to external JSON files")
    for i, (seed_prompt, data) in tqdm(enumerate(zip(prompts, generated_data)), total=len(generated_data), desc="to JSON file"):
        if data:
            data["seed_prompt"] = seed_prompt
            filename = f"{save_dir_path}/generated_data_{i}.json"
            filenames.append(filename)

            with open(filename, "w") as f:
                json.dump(data, f)

    return filenames

def collage_as_dataset(
    filenames, service_model_name, topic, split
):
    """
    collage_as_dataset loads up all JSON files produced by synth_data_generation,
    then re-organized into Hugging Face Dataset with additional information. 
    Also the messages are reformatted as standard message format as
    ([{'role': ... 'content': ...}]).
    """    
    all_json_dicts = _load_all_json_files(filenames)

    all_formatted_responses = []
    seed_prompts = []
    for json_dict in all_json_dicts:
        partial_seed_prompts, formatted_responses = _format_response(json_dict)
        for formatted_response in formatted_responses:
            all_formatted_responses.append(formatted_response)
            seed_prompts.append(partial_seed_prompts)
    
    generators = [service_model_name] * len(all_formatted_responses)
    prompt_ids = ["gemini-generated"] * len(all_formatted_responses)
    categories = [topic] * len(all_formatted_responses)
    dataset_train = Dataset.from_dict(
        {
            "generators": generators,
            "prompt_id": prompt_ids,
            "seed_prompts": seed_prompts,
            "messages": all_formatted_responses, 
            "category": categories
        }
    )
    return DatasetDict(
        {split: dataset_train}
    )