import argparse
import json
import os

import openai
import urllib3
from dotenv import load_dotenv

from ask_embeddings import (base64_from_vector, get_completion_with_context,
                            get_embedding, Library, get_context_for_library,
                            get_chunk_infos_for_library, CURRENT_VERSION,
                            EMBEDDINGS_MODEL_ID)

# TODO: Make this computed from the number of servers.
CONTEXT_TOKEN_COUNT = 1500

DEFAULT_CONFIG_FILE = "client.SECRET.json"


def query_server(query_embedding, random, server):
    http = urllib3.PoolManager()
    fields = {
        "version": CURRENT_VERSION,
        "access_token": server_tokens.get(server, ''),
        "query_embedding_model": EMBEDDINGS_MODEL_ID,
        "count": CONTEXT_TOKEN_COUNT
    }
    if random:
        fields["sort"] = "random"
    else:
        fields["query_embedding"] = query_embedding
    response = http.request(
        'POST', server, fields=fields).data
    obj = json.loads(response)
    if 'error' in obj:
        error = obj['error']
        raise Exception(f"Server returned an error: {error}")
    return Library(data=obj)


parser = argparse.ArgumentParser()
parser.add_argument("query", help="The question to ask",
                    default="Tell me about 3P")
parser.add_argument("--dev", action="store_true",
                    help=f"If set, will use the dev_* properties for each endpoint in config if they exist")
parser.add_argument("--config", help=f"A path to a config file to use. If not provided it will try to use {DEFAULT_CONFIG_FILE} if it exists. Pass \"\" explicitly to proactively ignore that file even if it exists", default=None)
parser.add_argument("--server", help="A server to use for querying",
                    action="append"),
parser.add_argument("--completion", help="Request completion based on the query and context",
                    action=argparse.BooleanOptionalAction, default=True)
parser.add_argument("--random", help="Ask for a random set of chunks",
                    action=argparse.BooleanOptionalAction, default=False)
parser.add_argument("--verbose", help="Print out context and sources and other useful intermediate data",
                    action=argparse.BooleanOptionalAction, default=False)
args = parser.parse_args()

config = {}

config_file = args.config
complain_for_missing_config = True
if config_file == None:
    config_file = DEFAULT_CONFIG_FILE
    complain_for_missing_config = False

if config_file:
    if os.path.exists(config_file):
        print(f'Using config {config_file}')
        with open(config_file, 'r') as f:
            config = json.load(f)
    else:
        if complain_for_missing_config:
            print(f'{config_file} was not found.')
    
load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")

query = args.query
server_list = args.server
dev_mode = args.dev

if not server_list:
    server_list = []

server_tokens = {}

if 'servers' in config:
    for server_config in config['servers'].values():
        endpoint = server_config.get('endpoint', '')
        if dev_mode and 'dev_endpoint' in server_config:
            endpoint = server_config['dev_endpoint']
        if not endpoint:
            continue
        server_list.append(endpoint)
        server_tokens[endpoint] = server_config.get('token', '')

if len(server_list) == 0:
    print('No servers provided.')

if args.verbose:
    if args.random:
        print("Getting random chunks ...")
    else:
        print(f"Getting embedding for \"{query}\" ...")

query_vector = None if args.random else base64_from_vector(
    get_embedding(query))

context = []
sources = []
for server in server_list:
    print(f"Querying {server} ...") if args.verbose else None
    # for now, just combine contexts
    library = query_server(query_vector, args.random, server)
    if library.message:
        print(f'{server} said: ' + library.message)
    context.extend(get_context_for_library(library))
    sources.extend([chunk["url"]
                   for chunk in get_chunk_infos_for_library(library)])

sources = "\n  ".join(sources)

if args.verbose:
    context_str = "\n\n".join(context)
    print(f"Context:\n{context_str}")
    print(f"\nSources:\n  {sources}")

if args.completion:
    print("Getting completion ...") if args.verbose else None
    print(f"\nAnswer:\n{get_completion_with_context(query, context)}")
    print(f"\nSources:\n  {sources}")
