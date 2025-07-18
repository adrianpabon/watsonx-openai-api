from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
import requests
import httpx
import os
import time
import uuid
import logging
import json
from tabulate import tabulate
from ibm_watsonx_ai import APIClient, Credentials
from llama_index.core.llms import ChatMessage
from llama_index.llms.ibm import WatsonxLLM
from dotenv import load_dotenv
import asyncio
import re

load_dotenv()

app = FastAPI()

# Function to check if the app is running inside Docker
def is_running_in_docker():
    """Check if the app is running inside Docker by checking for specific environment variables or Docker files."""
    return os.path.exists('/.dockerenv') or os.getenv("DOCKER") == "true"

# Logging configuration
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Mapping of valid regions
valid_regions = ["us-south", "eu-gb", "jp-tok", "eu-de"]

# Get region from env variable
region = os.getenv("WATSONX_REGION")

api_version = os.getenv("WATSONX_VERSION") or "2023-05-29"

# Handle behavior based on environment (Docker vs. Interactive mode)
if not region or region not in valid_regions:
    if is_running_in_docker():
        # In Docker, raise an error if WATSONX_REGION is missing or invalid
        logger.error(f"WATSONX_REGION key is not set or invalid. Supported regions are: {', '.join(valid_regions)}.")
        raise SystemExit(f"WATSONX_REGION is required. Supported regions are: {', '.join(valid_regions)}.")
    else:
        # In interactive mode, prompt the user for the region
        print("Please select a region from the following options:")
        for idx, reg in enumerate(valid_regions, start=1):
            print(f"{idx}. {reg}")

        choice = input("Enter the number corresponding to your region: ")

        try:
            region = valid_regions[int(choice) - 1]
        except (IndexError, ValueError):
            raise ValueError("Invalid region selection. Please restart and select a valid option.")

# Mapping of each region to URL
WATSONX_URLS = {
    "us-south": "https://us-south.ml.cloud.ibm.com",
    "eu-gb": "https://eu-gb.ml.cloud.ibm.com",
    "jp-tok": "https://jp-tok.ml.cloud.ibm.com",
    "eu-de": "https://eu-de.ml.cloud.ibm.com"
}

# Set the Watsonx URL based on the selected region


# IBM Cloud IAM URL for fetching the token
IAM_TOKEN_URL = "https://iam.cloud.ibm.com/identity/token"

# Load IBM API key, Watsonx URL, and Project ID from environment variables
IBM_API_KEY = os.getenv("WATSONX_IAM_APIKEY")
WATSONX_MODELS_URL = f"{WATSONX_URLS.get(region)}/ml/v1/foundation_model_specs"
PROJECT_ID = os.getenv("WATSONX_PROJECT_ID")

# Construct Watsonx URLs with the version parameter
WATSONX_URL = f"{WATSONX_URLS.get(region)}/ml/v1/text/generation?version={api_version}"
WATSONX_URL_CHAT = f"{WATSONX_URLS.get(region)}/ml/v1/text/chat?version={api_version}"
WATSONX_URL_STREAM = f"{WATSONX_URLS.get(region)}/ml/v1/text/chat_stream?version={api_version}"

if not IBM_API_KEY:
    logger.error("IBM API key is not set. Please set the WATSONX_IAM_APIKEY environment variable.")
    raise SystemExit("IBM API key is required.")

if not PROJECT_ID:
    logger.error("Watsonx.ai project ID is not set. Please set the WATSONX_PROJECT_ID environment variable.")
    raise SystemExit("Watsonx.ai project ID is required.")

# Global variables to cache the IAM token and expiration time
cached_token = None
token_expiration = 0

# Function to fetch the IAM token
def get_iam_token():
    global cached_token, token_expiration
    current_time = time.time()

    if cached_token and current_time < token_expiration:
        logger.debug("Using cached IAM token.")
        return cached_token

    logger.debug("Fetching new IAM token from IBM Cloud...")

    try:
        response = requests.post(
            IAM_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
                "apikey": IBM_API_KEY,
            },
        )
        response.raise_for_status()
        token_data = response.json()
        cached_token = token_data["access_token"]
        expires_in = token_data["expires_in"]

        token_expiration = current_time + expires_in - 600
        logger.debug(f"IAM token fetched, expires in {expires_in} seconds.")

        return cached_token
    except requests.exceptions.RequestException as err:
        logger.error(f"Error fetching IAM token: {err}")
        raise HTTPException(status_code=500, detail=f"Error fetching IAM token: {err}")

def format_debug_output(request_data):
    headers = ["by API", "Parameter", "API Value", "Default Value", "Explanation"]
    table = []
    # Define the parameters excluding "Prompt" and add explanations
    parameters = [
        ("Model ID", request_data.get("model", "ibm/granite-20b-multilingual"), "ibm/granite-20b-multilingual",
        "ID of the model to use for completion"),

        ("Max Tokens", request_data.get("max_tokens", 2000), 2000,
        "Maximum number of tokens to generate in the completion. The total tokens, prompt + completion."),

        ("Temperature", request_data.get("temperature", 0.2), 0.2,
        "Controls the randomness of the generated output. Higher values make the output more random."),

        ("Presence Penalty", request_data.get("presence_penalty", 1), 1,
        "Penalizes new tokens based on whether they appear in the text so far. Positive values encourage the model to talk about new topics."),

        ("top_p", request_data.get("top_p", 1), 1,
        "Nucleus sampling parameter. For example, top_p = 0.1 means the model will consider only the top 10% probability tokens."),

        ("best_of", request_data.get("best_of", 1), 1,
        "Generates multiple completions server-side, returning the 'best' one (the one with the highest log probability)."),

        ("echo", request_data.get("echo", False), False,
        "If set to True, echoes the prompt back along with the completion. Useful for debugging purposes."),

        ("n", request_data.get("n", 1), 1,
        "Number of completions to generate for each prompt. Note that this can quickly consume your token quota."),

        ("seed", request_data.get("seed", None), None,
        "If specified, ensures deterministic outputs, meaning repeated requests with the same seed and parameters should return the same result."),

        ("stop", request_data.get("stop", None), None,
        "Up to 4 sequences where the model will stop generating further tokens. The generated text will not contain the stop sequence."),

        ("logit_bias", request_data.get("logit_bias", None), None,
        "A JSON object that adjusts the likelihood of specified tokens appearing in the completion. Maps token IDs to a bias value from -100 to 100."),

        ("logprobs", request_data.get("logprobs", None), None,
        "Includes the log probabilities on the logprobs most likely tokens, as well as the chosen tokens. Useful for analyzing the model's decision process."),

        ("stream", request_data.get("stream", False), False,
        "If set to True, streams back partial progress as the model generates tokens in real-time."),

        ("suffix", request_data.get("suffix", None), None,
        "Specifies a suffix that comes after the generated text. Useful for inserting text after a completion.")
    ]


    # ANSI escape codes for colors
    green = "\033[92m"
    yellow = "\033[93m"
    reset = "\033[0m"

    for param, api_value, default_value, explanation in parameters:
        # Determine if the parameter was provided by the API (yellow) or using default (green)
        if api_value is not None:
            # Use yellow for rows where the API value differs from the default
            color = yellow
            provided_by_api = f"{yellow}X{reset}"
        else:
            # Use green for rows with default values
            color = green
            provided_by_api = ""

        # Append the row with color applied to the entire line
        table.append([
            provided_by_api,  # First column: Provided by API
            f"{color}{param}{reset}",
            f"{color}\"{api_value}\"{reset}" if isinstance(api_value, str) else f"{color}{api_value}{reset}",
            f"{color}{default_value}{reset}",
            f"{color}{explanation}{reset}"
        ])

    # Align the "Provided by API" and "Parameter" columns to the left, as well as "Explanation"
    return tabulate(table, headers, tablefmt="pretty", colalign=("center", "left", "center", "center", "left"))


# Fetch the models from Watsonx
def get_watsonx_models():
    try:
        token = get_iam_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        # Send request to Watsonx models endpoint
        params = {
            "version": "2024-09-16",  # Ensure the version is correct
            "filters": "function_text_generation,!lifecycle_withdrawn:and",
            "limit": 200
        }
        response = requests.get(
            WATSONX_MODELS_URL,
            headers=headers,
            params=params
        )

        if response.status_code == 404:
            logger.error("404 Not Found: The endpoint or version might be incorrect.")
            raise HTTPException(status_code=404, detail="Watsonx Models API endpoint not found.")

        response.raise_for_status()  # Raise exception for any non-200 status codes
        models_data = response.json()
        return models_data
    except requests.exceptions.RequestException as err:
        logger.error(f"Error fetching models from Watsonx.ai: {err}")
        raise HTTPException(status_code=500, detail=f"Error fetching models from Watsonx.ai: {err}")


# Convert Watsonx models to OpenAI-like format
def convert_watsonx_to_openai_format(watsonx_data):
    openai_models = []

    for model in watsonx_data['resources']:
        openai_model = {
            "id": model['model_id'],  # Watsonx's model_id maps to OpenAI's id
            "object": "model",  # Hardcoded, as OpenAI uses "model" as the object type
            "created": int(time.time()),  # Optional: use current timestamp or a fixed one if available
            "owned_by": f"{model['provider']} / {model['source']}",  # Combine Watsonx's provider and source
            "description": f"{model['short_description']} Supports tasks like {', '.join(model.get('task_ids', []))}.",  # Watsonx's short description
            "max_tokens": model['model_limits']['max_output_tokens'],  # Map Watsonx's max_output_tokens to OpenAI's max_tokens
            "token_limits": {
                "max_sequence_length": model['model_limits']['max_sequence_length'],  # Watsonx's max_sequence_length
                "max_output_tokens": model['model_limits']['max_output_tokens']  # Watsonx's max_output_tokens
            }
        }
        openai_models.append(openai_model)

    return {
        "data": openai_models
    }


# FastAPI route for /v1/models
@app.get("/v1/chat/models")
async def fetch_models():
    logger.info("get the model list")
    try:
        models = get_watsonx_models()  # Fetch the Watsonx models data
        logger.debug(f"Available models: {models}")

        # Convert Watsonx output to OpenAI-like format
        openai_like_models = convert_watsonx_to_openai_format(models)

        # Return the OpenAI-like formatted models
        return openai_like_models
    except Exception as err:
        logger.error(f"Error fetching models: {err}")
        raise HTTPException(status_code=500, detail=f"Error fetching models: {err}")

# FastAPI route for /v1/models/{model_id}
@app.get("/v1/chat/models/{model_id}")
async def fetch_model_by_id(model_id: str):
    logger.info(f"get the model with id {model_id}")
    try:
        # Fetch the full list of models from Watsonx
        models = get_watsonx_models()

        # Search for the model with the specific model_id
        model = next((m for m in models['resources'] if m['model_id'] == model_id), None)

        if model:
            # Convert the model details to OpenAI-like format
            openai_model = {
                "id": model['model_id'],  # Watsonx's model_id maps to OpenAI's id
                "object": "model",  # Hardcoded, as OpenAI uses "model" as the object type
                "created": int(time.time()),  # Optional: use current timestamp or a fixed one if available
                "owned_by": f"{model['provider']} / {model['source']}",  # Combine Watsonx's provider and source
                "description": f"{model['short_description']} Supports tasks like {', '.join(model.get('task_ids', []))}.",  # Watsonx's short description
                "max_tokens": model['model_limits']['max_output_tokens'],  # Map Watsonx's max_output_tokens to OpenAI's max_tokens
                "token_limits": {
                    "max_sequence_length": model['model_limits']['max_sequence_length'],  # Watsonx's max_sequence_length
                    "max_output_tokens": model['model_limits']['max_output_tokens']  # Watsonx's max_output_tokens
                }
            }
            return {"data": [openai_model]}

        # If model not found, raise a 404 error
        raise HTTPException(status_code=404, detail=f"Model with ID {model_id} not found.")

    except Exception as err:
        logger.error(f"Error fetching model by ID: {err}")
        raise HTTPException(status_code=500, detail=f"Error fetching model by ID: {err}")

@app.post("/v1/completions")
async def watsonx_completions(request: Request):
    logger.info("Received a Watsonx completion request.")

    # Parse the incoming request as JSON
    try:
        request_data = await request.json()
    except Exception as e:
        logger.error(f"Error parsing request: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON request body")

    # Extract parameters from request or set default values
    prompt = request_data.get("prompt", "")

    # Ensure that prompt is a string; if it's a list, join it into a single string
    if isinstance(prompt, list):
        prompt = " ".join(prompt)
    elif not isinstance(prompt, str):
        logger.error(f"Invalid type for 'prompt': {type(prompt)}. Expected a string or list of strings.")
        raise HTTPException(status_code=400, detail="Invalid type for 'prompt'. Expected a string or list of strings.")

    # Rest of the parameters (model_id, max_tokens, etc.)
    model_id = request_data.get("model", "ibm/granite-20b-multilingual")  # Default model_id
    max_tokens = request_data.get("max_tokens", 2000)
    temperature = request_data.get("temperature", 0.2)
    best_of = request_data.get("best_of", 1)
    n = request_data.get("n", 1)
    presence_penalty = request_data.get("presence_penalty", 1)
    echo = request_data.get("echo", False)
    logit_bias = request_data.get("logit_bias", None)
    logprobs = request_data.get("logprobs", None)
    stop = request_data.get("stop", None)
    suffix = request_data.get("suffix", None)
    stream = request_data.get("stream", False)
    seed = request_data.get("seed", None)
    top_p = request_data.get("top_p", 1)

    # Handle tool calling parameters that n8n/LangChain might send
    tools = request_data.get("tools", None)
    functions = request_data.get("functions", None)  # Legacy functions parameter
    tool_choice = request_data.get("tool_choice", None)
    function_call = request_data.get("function_call", None)  # Legacy function_call parameter
    
    # Log if tool calling is being attempted
    if tools or functions or tool_choice or function_call:
        logger.info(f"Tool calling detected - tools: {tools}, functions: {functions}, tool_choice: {tool_choice}, function_call: {function_call}")
        logger.warning("Tool calling is not fully supported by Watsonx. Converting to regular chat completion.")

    # Debugging: Log the provided parameters and their sources
    logger.debug("Parameter source debug:")
    logger.debug("\n" + format_debug_output(request_data))

    # Get the IAM token
    iam_token = get_iam_token()

    # Prepare Watsonx.ai request payload
    watsonx_payload = {
        "input": prompt,  # Ensure 'prompt' is always a string
        "parameters": {
            "decoding_method": "sample",  # decoding_method = Greedy is not supported.
            "max_new_tokens": max_tokens,
            "temperature": temperature,
            "top_k": 50,
            "top_p": top_p,
            "random_seed": seed,
            "repetition_penalty": presence_penalty,
        },
        "model_id": model_id,
        "project_id": PROJECT_ID
    }

    # Optionally add optional parameters if provided
    if stop:
        watsonx_payload["parameters"]["stop_sequences"] = stop
    if logit_bias:
        watsonx_payload["parameters"]["logit_bias"] = logit_bias

    # Log the prettified JSON request
    formatted_payload = json.dumps(watsonx_payload, indent=4, ensure_ascii=False)
    logger.debug(f"Sending request to Watsonx.ai: {formatted_payload}")

    headers = {
        "Authorization": f"Bearer {iam_token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    try:
        # Send the request to Watsonx.ai
        response = requests.post(WATSONX_URL, json=watsonx_payload, headers=headers)
        response.raise_for_status()  # This will raise an HTTPError for 4xx/5xx responses
        watsonx_data = response.json()
        logger.debug(f"Received response from Watsonx.ai: {json.dumps(watsonx_data, indent=4)}")
    except requests.exceptions.HTTPError as err:
        # Capture and log the full response from Watsonx.ai
        error_message = response.text  # Watsonx should return a more detailed error message
        logger.error(f"HTTPError: {err}, Response: {error_message}")
        raise HTTPException(status_code=response.status_code, detail=f"Error from Watsonx.ai: {error_message}")
    except requests.exceptions.RequestException as err:
        # Generic request exception handling
        logger.error(f"RequestException: {err}")
        raise HTTPException(status_code=500, detail=f"Error calling Watsonx.ai: {err}")

    # Extract generated text from the Watsonx response
    results = watsonx_data.get("results", [])
    if results and "generated_text" in results[0]:
        generated_text = results[0]["generated_text"]
        logger.debug(f"Generated text from Watsonx.ai: \n{generated_text}")
    else:
        generated_text = "\n\nNo response available."
        logger.warning("No generated text found in Watsonx.ai response.")

    # Prepare the OpenAI-compatible response with model name
    openai_response = {
        "id": f"cmpl-{str(uuid.uuid4())[:12]}",
        "object": "text_completion",
        "created": int(time.time()),
        "model": model_id,
        "system_fingerprint": f"fp_{str(uuid.uuid4())[:12]}",
        "choices": [
            {
                "text": generated_text,
                "index": 0,
                "logprobs": None,
                "finish_reason": results[0].get("stop_reason", "length")
            }
        ],
        "usage": {
            "prompt_tokens": results[0].get("input_token_count", 5),
            "completion_tokens": results[0].get("generated_token_count", 7),
            "total_tokens": results[0].get("input_token_count", 5) + results[0].get("generated_token_count", 7)
        }
    }

    # Return the response
    logger.debug(f"Returning OpenAI-compatible response: {json.dumps(openai_response, indent=4)}")
    return openai_response


async def stream_watsonx_completions(watsonx_payload, headers):
    logger.info("Stream chat completion request.")
    async with httpx.AsyncClient(timeout=120.0) as client:  # Aumentar timeout
        try:
            # Send the request to Watsonx.ai
            response = await client.post(WATSONX_URL_STREAM, json=watsonx_payload, headers=headers)
            response.raise_for_status()
            
            # Generate unique ID for the response
            response_id = f"chatcmpl-{str(uuid.uuid4())[:12]}"
            created_time = int(time.time())
            model = watsonx_payload.get("model_id", "ibm/granite-20b-multilingual")
            
            # Send initial chunk
            initial_chunk = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created_time,
                "model": model,
                "system_fingerprint": f"fp_{str(uuid.uuid4())[:12]}",
                "choices": [{
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": None,
                        "function_call": None
                    },
                    "logprobs": None,
                    "finish_reason": None
                }],
                "usage": None
            }
            
            yield f"data: {json.dumps(initial_chunk)}\n\n".encode('utf-8')
            
            # Process the streaming response
            accumulated_content = ""
            
            async for chunk in response.aiter_bytes():
                chunk_str = chunk.decode('utf-8').strip()
                logger.debug(f"Received chunk: {chunk_str}")
                
                # Process each line in the chunk
                for line in chunk_str.split('\n'):
                    line = line.strip()
                    if line.startswith('data:'):
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            # Send final chunk
                            final_chunk = {
                                "id": response_id,
                                "object": "chat.completion.chunk",
                                "created": created_time,
                                "model": model,
                                "system_fingerprint": f"fp_{str(uuid.uuid4())[:12]}",
                                "choices": [{
                                    "index": 0,
                                    "delta": {},
                                    "logprobs": None,
                                    "finish_reason": "stop"
                                }],
                                "usage": None
                            }
                            yield f"data: {json.dumps(final_chunk)}\n\n".encode('utf-8')
                            yield b"data: [DONE]\n\n"
                            return
                        
                        try:
                            data_json = json.loads(data_str)
                            
                            # Extract content from watsonx response
                            choices = data_json.get('choices', [])
                            if choices and len(choices) > 0:
                                choice = choices[0]
                                if 'message' in choice:
                                    content = choice['message'].get('content', '')
                                elif 'delta' in choice:
                                    content = choice['delta'].get('content', '')
                                else:
                                    continue
                                
                                if content:
                                    accumulated_content += content
                                    
                                    # Create OpenAI format chunk
                                    chunk_response = {
                                        "id": response_id,
                                        "object": "chat.completion.chunk",
                                        "created": created_time,
                                        "model": model,
                                        "system_fingerprint": f"fp_{str(uuid.uuid4())[:12]}",
                                        "choices": [{
                                            "index": 0,
                                            "delta": {
                                                "content": content
                                            },
                                            "logprobs": None,
                                            "finish_reason": None
                                        }],
                                        "usage": None
                                    }
                                    
                                    yield f"data: {json.dumps(chunk_response)}\n\n".encode('utf-8')
                                    
                        except json.JSONDecodeError as e:
                            logger.error(f"JSONDecodeError: {e}, data: {data_str}")
                            continue
                            
            # Fallback: if no proper streaming chunks received, create a simple response
            if not accumulated_content:
                simple_chunk = {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created_time,
                    "model": model,
                    "system_fingerprint": f"fp_{str(uuid.uuid4())[:12]}",
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "content": "I understand your request."
                        },
                        "logprobs": None,
                        "finish_reason": None
                    }],
                    "usage": None
                }
                yield f"data: {json.dumps(simple_chunk)}\n\n".encode('utf-8')
                
                # Send finish chunk
                final_chunk = {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created_time,
                    "model": model,
                    "system_fingerprint": f"fp_{str(uuid.uuid4())[:12]}",
                    "choices": [{
                        "index": 0,
                        "delta": {},
                        "logprobs": None,
                        "finish_reason": "stop"
                    }],
                    "usage": None
                }
                yield f"data: {json.dumps(final_chunk)}\n\n".encode('utf-8')
                
            yield b"data: [DONE]\n\n"
                            
        except httpx.ReadTimeout as err:
            logger.error(f"ReadTimeout: {err}")
            # Return error in OpenAI streaming format
            error_chunk = {
                "id": f"chatcmpl-{str(uuid.uuid4())[:12]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": watsonx_payload.get("model_id", "ibm/granite-20b-multilingual"),
                "choices": [{
                    "index": 0,
                    "delta": {
                        "content": "I apologize, but I'm having trouble processing your request right now. Please try again."
                    },
                    "logprobs": None,
                    "finish_reason": "stop"
                }]
            }
            yield f"data: {json.dumps(error_chunk)}\n\n".encode('utf-8')
            yield b"data: [DONE]\n\n"
            
        except httpx.HTTPStatusError as err:
            logger.error(f"HTTPError: {err}")
            error_message = await response.aread() if hasattr(response, 'aread') else "Unknown error"
            logger.error(f"Response: {error_message}")
            
            error_chunk = {
                "id": f"chatcmpl-{str(uuid.uuid4())[:12]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": watsonx_payload.get("model_id", "ibm/granite-20b-multilingual"),
                "choices": [{
                    "index": 0,
                    "delta": {
                        "content": "I encountered an error while processing your request. Please try again later."
                    },
                    "logprobs": None,
                    "finish_reason": "stop"
                }]
            }
            yield f"data: {json.dumps(error_chunk)}\n\n".encode('utf-8')
            yield b"data: [DONE]\n\n"
            
        except Exception as err:
            logger.error(f"Unexpected error: {err}")
            error_chunk = {
                "id": f"chatcmpl-{str(uuid.uuid4())[:12]}",
                "object": "chat.completion.chunk", 
                "created": int(time.time()),
                "model": watsonx_payload.get("model_id", "ibm/granite-20b-multilingual"),
                "choices": [{
                    "index": 0,
                    "delta": {
                        "content": "An unexpected error occurred. Please try again."
                    },
                    "logprobs": None,
                    "finish_reason": "stop"
                }]
            }
            yield f"data: {json.dumps(error_chunk)}\n\n".encode('utf-8')
            yield b"data: [DONE]\n\n"

async def non_stream_watsonx_completions(watsonx_payload, headers):
    try:
        # Send the request to Watsonx.ai
        response = requests.post(WATSONX_URL_CHAT, json=watsonx_payload, headers=headers)
        response.raise_for_status()  # This will raise an HTTPError for 4xx/5xx responses
        watsonx_data = response.json()
        logger.debug(f"Received response from Watsonx.ai: {json.dumps(watsonx_data, indent=4)}")
        
        # Convert Watsonx response to OpenAI chat completions format
        openai_response = {
            "id": f"chatcmpl-{str(uuid.uuid4())[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": watsonx_payload.get("model_id", "ibm/granite-20b-multilingual"),
            "system_fingerprint": f"fp_{str(uuid.uuid4())[:12]}",
            "choices": [],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0
            }
        }
        
        # Extract choices from Watsonx response
        choices = watsonx_data.get("choices", [])
        if choices:
            for i, choice in enumerate(choices):
                message_content = choice.get("message", {}).get("content", "")
                finish_reason = choice.get("finish_reason", "stop")
                
                # Ensure content is never None
                if message_content is None:
                    message_content = ""
                
                openai_choice = {
                    "index": i,
                    "message": {
                        "role": "assistant",
                        "content": message_content,
                        "tool_calls": None,
                        "function_call": None
                    },
                    "logprobs": None,
                    "finish_reason": finish_reason
                }
                openai_response["choices"].append(openai_choice)
        else:
            # Fallback: create a single choice with proper content
            openai_response["choices"] = [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "I understand your request and I'm here to help you.",
                    "tool_calls": None,
                    "function_call": None
                },
                "logprobs": None,
                "finish_reason": "stop"
            }]
        
        # Extract usage information if available
        usage = watsonx_data.get("usage", {})
        if usage:
            openai_response["usage"] = {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0)
            }
        
        logger.debug(f"Converted to OpenAI format: {json.dumps(openai_response, indent=4)}")
        return openai_response
        
    except requests.exceptions.HTTPError as err:
        # Capture and log the full response from Watsonx.ai
        error_message = response.text  # Watsonx should return a more detailed error message
        logger.error(f"HTTPError: {err}, Response: {error_message}")
        raise HTTPException(status_code=response.status_code, detail=f"Error from Watsonx.ai: {error_message}")
    except requests.exceptions.RequestException as err:
        # Generic request exception handling
        logger.error(f"RequestException: {err}")
        raise HTTPException(status_code=500, detail=f"Error calling Watsonx.ai: {err}")

@app.post("/v1/chat/completions")
async def watsonx_completions(request: Request):
    logger.info("Received a Watsonx chat completion request.")
    # Parse the incoming request as JSON
    try:
        request_data = await request.json()
        logger.info(f"request_data is {request_data}")
    except Exception as e:
        logger.error(f"Error parsing request: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON request body")

    messages = request_data.get("messages", [])
    # Rest of the parameters (model_id, max_tokens, etc.)
    model_id = request_data.get("model", "ibm/granite-20b-multilingual")  # Default model_id
    max_tokens = request_data.get("max_tokens", 2000)
    temperature = request_data.get("temperature", 0.2)
    best_of = request_data.get("best_of", 1)
    n = request_data.get("n", 1)
    presence_penalty = request_data.get("presence_penalty", 1)
    echo = request_data.get("echo", False)
    logit_bias = request_data.get("logit_bias", None)
    logprobs = request_data.get("logprobs", None)
    stop = request_data.get("stop", None)
    suffix = request_data.get("suffix", None)
    stream = request_data.get("stream", False)
    seed = request_data.get("seed", None)
    top_p = request_data.get("top_p", 1)

    # Handle tool calling parameters that n8n/LangChain might send
    tools = request_data.get("tools", None)
    functions = request_data.get("functions", None)  # Legacy functions parameter
    tool_choice = request_data.get("tool_choice", None)
    function_call = request_data.get("function_call", None)  # Legacy function_call parameter
    
    # Log if tool calling is being attempted
    if tools or functions or tool_choice or function_call:
        logger.info(f"Tool calling detected - tools: {tools}, functions: {functions}, tool_choice: {tool_choice}, function_call: {function_call}")
        logger.warning("Tool calling is not fully supported by Watsonx. Converting to regular chat completion.")

    # Debugging: Log the provided parameters and their sources
    logger.debug("Parameter source debug:")
    logger.debug("\n" + format_debug_output(request_data))

    # Get the IAM token
    iam_token = get_iam_token()

    # Prepare Watsonx.ai request payload
    watsonx_payload = {
        "messages": messages,
        "seed":seed,
        "frequency_penalty": presence_penalty,
        "model_id": model_id,
        "project_id": PROJECT_ID,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
        # "time_limit": 1000
    }

    # Optionally add optional parameters if provided
    if stop:
        watsonx_payload["stop"] = stop
    if logit_bias:
        watsonx_payload["logit_bias"] = logit_bias

    # Log the prettified JSON request
    formatted_payload = json.dumps(watsonx_payload, indent=4, ensure_ascii=False)
    logger.debug(f"Sending request to Watsonx.ai: {formatted_payload}")

    headers = {
        "Authorization": f"Bearer {iam_token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    # stream = False # make always not stream as issues for watsonx
    if stream:
        return StreamingResponse(
            stream_watsonx_completions(watsonx_payload, headers), 
            media_type="text/plain",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Content-Type": "text/plain; charset=utf-8"
            }
        )
    else:
        return await non_stream_watsonx_completions(watsonx_payload, headers)


@app.post("/v1/llamaindex/chat/completions")
async def watsonx_chat_completions(request: Request):
    logger.info("Received a Watsonx chat completion request.")
    # Parse the incoming request as JSON
    try:
        request_data = await request.json()
        logger.info(f"request_data is {request_data}")
    except Exception as e:
        logger.error(f"Error parsing request: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON request body")

    model_id = request_data.get("model", "ibm/granite-20b-multilingual")  # Default model_id
    max_tokens = request_data.get("max_tokens", 2048)
    temperature = request_data.get("temperature", 0.2)
    presence_penalty = request_data.get("presence_penalty", 1)
    if presence_penalty < 1:
        presence_penalty = presence_penalty + 1
    seed = request_data.get("seed", None)
    top_p = request_data.get("top_p", 1)

    # Debugging: Log the provided parameters and their sources
    # logger.debug("Parameter source debug:")
    # logger.debug("\n" + format_debug_output(request_data))

    watsonx_llm = WatsonxLLM(
        model_id=model_id,
        # url=WATSONX_URL_CHAT,
        url=WATSONX_URLS.get(region),
        project_id=PROJECT_ID,
        temperature=temperature,
        max_new_tokens=max_tokens,
        repetition_penalty=presence_penalty,
        random_seed=seed,
        apikey=IBM_API_KEY,
        top_p=top_p
    )

    # Extract parameters from request or set default values
    messages = [ChatMessage(**t) for t in request_data.get("messages", [])]
    response = watsonx_llm.chat(
        messages, max_new_tokens=max_tokens, decoding_method="greedy"
    )
    logger.info(f"Returning OpenAI-compatible ChatResponse: {response}")
    return response.raw
    # return result
    # for chunk in await watsonx_llm.stream_chat(messages):
    #     print(chunk.delta, end="")
    #     yield chunk.delta

# Add compatibility route for n8n which expects /v1/chat/chat/completions
@app.post("/v1/chat/chat/completions")
async def watsonx_chat_chat_completions(request: Request):
    """Compatibility endpoint for n8n which expects /v1/chat/chat/completions path"""
    logger.info("Received a Watsonx chat completion request via /v1/chat/chat/completions (n8n compatibility route).")
    return await watsonx_completions(request)

# Optimized endpoint for N8N AI Agents
@app.post("/v1/n8n/chat/completions")
async def n8n_optimized_completions(request: Request):
    """
    Optimized endpoint for N8N AI Agents with better tool calling support
    """
    logger.info("Received an N8N optimized chat completion request.")
    
    try:
        request_data = await request.json()
        logger.info(f"N8N request_data: {request_data}")
    except Exception as e:
        logger.error(f"Error parsing N8N request: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON request body")

    messages = request_data.get("messages", [])
    model_id = request_data.get("model", "ibm/granite-20b-multilingual")
    max_tokens = request_data.get("max_tokens", 2000)
    temperature = request_data.get("temperature", 0.7)
    stream = request_data.get("stream", False)
    top_p = request_data.get("top_p", 1)
    
    # Handle tool calling parameters
    tools = request_data.get("tools", None)
    tool_choice = request_data.get("tool_choice", None)
    
    if tools or tool_choice:
        logger.info("N8N Tool calling detected, adjusting response format")

    # Get the IAM token
    iam_token = get_iam_token()

    # Prepare Watsonx.ai request payload
    watsonx_payload = {
        "messages": messages,
        "model_id": model_id,
        "project_id": PROJECT_ID,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
    }

    headers = {
        "Authorization": f"Bearer {iam_token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    if stream:
        return StreamingResponse(
            stream_watsonx_completions(watsonx_payload, headers), 
            media_type="text/plain",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Content-Type": "text/plain; charset=utf-8"
            }
        )
    else:
        return await non_stream_watsonx_completions(watsonx_payload, headers)

# Add compatibility routes for n8n which might expect /v1/chat/chat/models
@app.get("/v1/chat/chat/models")
async def fetch_models_compatibility():
    """Compatibility endpoint for n8n which might expect /v1/chat/chat/models path"""
    logger.info("Received models request via /v1/chat/chat/models (n8n compatibility route).")
    return await fetch_models()

@app.get("/v1/chat/chat/models/{model_id}")
async def fetch_model_by_id_compatibility(model_id: str):
    """Compatibility endpoint for n8n which might expect /v1/chat/chat/models/{model_id} path"""
    logger.info(f"Received model by ID request via /v1/chat/chat/models/{model_id} (n8n compatibility route).")
    return await fetch_model_by_id(model_id)

# Add standard OpenAI endpoints that some clients expect
@app.get("/v1/models")
async def fetch_models_standard():
    """Standard OpenAI models endpoint"""
    logger.info("Received models request via /v1/models (standard OpenAI route).")
    return await fetch_models()

@app.get("/v1/models/{model_id}")
async def fetch_model_by_id_standard(model_id: str):
    """Standard OpenAI model by ID endpoint"""
    logger.info(f"Received model by ID request via /v1/models/{model_id} (standard OpenAI route).")
    return await fetch_model_by_id(model_id)