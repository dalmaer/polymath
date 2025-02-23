import base64
from random import shuffle
import os
import glob
import json
import copy
import random
import hashlib
from time import sleep

import numpy as np
import openai
from transformers import GPT2TokenizerFast

EMBEDDINGS_MODEL_ID = "openai.com:text-embedding-ada-002"
COMPLETION_MODEL_NAME = "text-davinci-003"

EXPECTED_EMBEDDING_LENGTH = {
    'openai.com:text-embedding-ada-002': 1536
}

SEPARATOR = "\n"
MAX_CONTEXT_LEN_IN_TOKENS = 2048

LIBRARY_DIR = 'libraries'
SAMPLE_LIBRARIES_FILE = 'sample-content.json'

CURRENT_VERSION = 0

DEFAULT_PRIVATE_ACCESS_TAG = 'unpublished'

DEFAULT_CONFIG_FILE = 'host.SECRET.json'

def permitted_access(access_token):
    """
    Returns the set of permitted access tags, whether to include restricted_count in the result,
    and a message to return in the library if any results were filtered.
    """

    # TODO: allow overriding this
    access_file = DEFAULT_CONFIG_FILE
    if not os.path.exists(access_file):
        return set([]), False, ""

    # TODO: don't load this file every time
    with open(DEFAULT_CONFIG_FILE, 'r') as f:
        data = json.load(f)

    restricted = data.get('restricted', {})
    include_restricted_count = restricted.get('count', False)
    restricted_message = restricted.get('message', "")

    if not access_token:
        return set([]), include_restricted_count, restricted_message

    if 'tokens' not in data:
        raise Exception(f'The data in {access_file} did not contain a key of "tokens" as expected')
    
    private_access_tag = data['default_private_access_tag'] if 'default_private_access_tag' in data else DEFAULT_PRIVATE_ACCESS_TAG

    token_record = None
    for record in data['tokens'].values():
        if 'token' not in record:
            continue
        if record['token'] == access_token:
            token_record = record
            break
    
    if not token_record:
        return set([]), include_restricted_count, restricted_message

    tags = token_record['access_tags'] if 'access_tags' in token_record else [private_access_tag]

    return set(tags), include_restricted_count, restricted_message


def canonical_id_for_chunk(chunk):
    text = chunk.get('text', '')
    info = chunk.get('info', {})
    url = info.get('url', {})
    return canonical_id(text, url)


def canonical_id(chunk_text, url=''):
    """
    Returns the canonical ID for a given chunk of text.

    Today using the canonical ID as a chunk ID is a best practice, but in upcoming versions it will be required.
    """
    message = url.strip() + '\n' + chunk_text.strip()
    message_bytes = message.encode()
    hash_object = hashlib.sha256()
    hash_object.update(message_bytes)
    return hash_object.hexdigest()


# In JS, the argument can be produced with with:
# ```
# btoa(String.fromCharCode(...(new Uint8Array(new Float32Array(data).buffer))));
# ```
# where `data` is an array of floats


def vector_from_base64(str):
    return np.frombuffer(base64.b64decode(str), dtype=np.float32)

# In JS, the argument can be produced with with:
# ```
# new Float32Array(new Uint8Array([...atob(encoded_data)].map(c => c.charCodeAt(0))).buffer);
# ```
# where `encoded_data` is a base64 string


def base64_from_vector(vector):
    data = np.array(vector, dtype=np.float32)
    return base64.b64encode(data)


def vector_similarity(x, y):
    return np.dot(np.array(x), np.array(y))


def get_embedding_model_name_from_id(model_id):
    return model_id.split(':')[1]


def get_embedding(text, model_id=EMBEDDINGS_MODEL_ID):
    # Occasionally, API returns an error.
    # Retry a few times before giving up.
    retry_count = 10
    while retry_count > 0:
        try:
            result = openai.Embedding.create(
                model=get_embedding_model_name_from_id(model_id),
                input=text
            )
            break
        except Exception as e:
            print(f'openai.Embedding.create error: {e}')
            print("Retrying in 20 seconds ...")
            sleep(20)
            retry_count -= 1
    return result["data"][0]["embedding"]


def load_data_file(file):
    with open(file, "r") as f:
        return json.load(f)


class Library:
    def __init__(self, data=None, blob=None, filename=None, access_tag=None):
        if filename:
            data = load_data_file(filename)
        if blob:
            data = json.loads(blob)
        if data:
            self._data = data
        else:
            self.reset()

        if access_tag == None and filename:
            next_directory_is_access_tag = False
            for directory in os.path.dirname(filename).split('/'):
                if next_directory_is_access_tag:
                    access_tag = directory
                    break
                if directory == 'access':
                    next_directory_is_access_tag = True
                else:
                    next_directory_is_access_tag = False

        if access_tag == True:
            access_tag = DEFAULT_PRIVATE_ACCESS_TAG

        for _, chunk in self._data['content'].items():
            if 'embedding' not in chunk:
                continue
            chunk['embedding'] = vector_from_base64(chunk['embedding'])

        if access_tag:
            for chunk_id in self.chunk_ids:
                self.set_chunk_field(chunk_id, access_tag=access_tag)

        self.validate()


    def validate(self):
        if self._data.get('version', -1) != CURRENT_VERSION:
            raise Exception('Version invalid')
        if self._data.get('embedding_model', '') != EMBEDDINGS_MODEL_ID:
            raise Exception('Invalid model name')
        expected_embedding_length = EXPECTED_EMBEDDING_LENGTH.get(
            self._data.get('embedding_model', ''), 0)
        omit_whole_chunks, fields_to_omit, _ = keys_to_omit(
            self._data.get('omit', ''))
        if omit_whole_chunks and len(self._data['content']):
            raise Exception(
                'omit configured to omit all chunks but they were present')
        for chunk_id, chunk in self._data['content'].items():
            for field in fields_to_omit:
                if field in chunk:
                    raise Exception(
                        f"Expected {field} to be omitted but it was included")
            if 'text' not in fields_to_omit and 'text' not in chunk:
                raise Exception(f'{chunk_id} is missing text')
            if 'embedding' not in fields_to_omit:
                if 'embedding' not in chunk:
                    raise Exception(f'{chunk_id} is missing embedding')
                if len(chunk['embedding']) != expected_embedding_length:
                    raise Exception(
                        f'{chunk_id} had the wrong length of embedding, expected {expected_embedding_length}')
            if 'token_count' not in fields_to_omit:
                if 'token_count' not in chunk:
                    raise Exception(f'{chunk_id} is missing token_count')
            # TODO: verify token_count is a reasonable length.
            if 'info' not in fields_to_omit:
                if 'info' not in chunk:
                    raise Exception(f'{chunk_id} is missing info')
                info = chunk['info']
                if 'url' not in info:
                    raise Exception(f'{chunk_id} info is missing required url')


    @property
    def version(self):
        return self._data['version']


    @version.setter
    def version(self, value):
        if not isinstance(value, int):
            raise TypeError('Version must be an integer')
        self._data['version'] = value


    @property
    def embedding_model(self):
        return self._data['embedding_model']


    @embedding_model.setter
    def embedding_model(self, value):
        if value != EMBEDDINGS_MODEL_ID:
            raise TypeError(f'The only supported value for embedding model is {EMBEDDINGS_MODEL_ID}')
        self._data['embedding_model'] = value


    @property
    def omit(self):
        """
        Returns either a string or an array of strings all of which are legal omit keys.
        """
        if 'omit' not in self._data:
            return ''
        return self._data['omit']


    @property
    def omit_whole_chunk(self):
        omit_whole_chunk, _, _ = keys_to_omit(self.omit)
        return omit_whole_chunk


    @property
    def fields_to_omit(self):
        _, fields_to_omit, _ = keys_to_omit(self.omit)
        return fields_to_omit


    @omit.setter
    def omit(self, value):
        _, _, canonical_value = keys_to_omit(value)
        self._data['omit'] = canonical_value


    @property
    def _details(self):
        if 'details' not in self._data:
            return {}
        return self._data['details']


    @_details.setter
    def _details(self, value):
        self._data['details'] = value


    @property
    def counts(self):
        details = self._details
        if 'counts' not in details:
            return {}
        return details['counts']


    @counts.setter
    def counts(self, value):
        self._details = self._details
        self._details['counts'] = value


    @property
    def count_chunks(self):
        counts = self.counts
        if 'chunks' not in counts:
            return 0
        return counts['chunks']


    @count_chunks.setter
    def count_chunks(self, value):
        self._details = self._details
        self.counts = self.counts
        self.counts['chunks'] = value


    @property
    def count_restricted(self):
        counts = self.counts
        if 'restricted' not in counts:
            return 0
        return counts['restricted']


    @count_restricted.setter
    def count_restricted(self, value):
        self._details = self._details
        self.counts = self.counts
        self.counts['restricted'] = value


    @property
    def message(self):
        details = self._details
        if 'message' not in details:
            return ''
        return details['message']

    
    @message.setter
    def message(self, value):
        self._details = self._details
        self._details['message'] = value


    def extend(self, other : 'Library'):
        if other.embedding_model != self.embedding_model:
            raise Exception('The other library had a different embedding model')
        # TODO: handle key collisions; keys are only guaranteed to be unique
        # within a single library.
        self._data['content'].update(other._data['content'])


    def reset(self):
        self._data = {
            'version': CURRENT_VERSION,
            'embedding_model': EMBEDDINGS_MODEL_ID,
            'content': {}
        }


    @property
    def chunk_ids(self):
        """
        Returns an iterator for the chunk_ids in the library in order.
        """
        return self._data["content"].keys()


    def chunk(self, chunk_id):
        if chunk_id not in self._data["content"]:
            return None
        return self._data["content"][chunk_id]


    @property
    def chunks(self):
        """
        Returns an iterator of (chunk_id, chunk)
        """
        return self._data["content"].items()


    def delete_chunk(self, chunk_id):
        del self._data["content"][chunk_id]


    def _strip_chunk(self, chunk):
        if self.omit_whole_chunk:
            chunk.clear()
        for field_to_omit in self.fields_to_omit:
            if field_to_omit in chunk:
                del chunk[field_to_omit]


    def set_chunk(self, chunk_id, chunk):
        if self.omit_whole_chunk:
            return
        self._data["content"][chunk_id] = chunk
        self._strip_chunk(chunk)


    def set_chunk_field(self, chunk_id, text=None, embedding=None, token_count=None, info=None, access_tag=None):
        if self.omit_whole_chunk:
            return
        if chunk_id not in self._data["content"]:
            self._data["content"][chunk_id] = {}
        chunk = self._data["content"][chunk_id]
        if text != None:
            chunk["text"] = text
        if embedding != None:
            chunk["embedding"] = embedding
        if token_count != None:
            chunk["token_count"] = token_count
        if info != None:
            chunk["info"] = info
        if access_tag != None:
            chunk["access_tag"] = access_tag
        self._strip_chunk(chunk)


    def delete_chunk_field(self, chunk_id, fields=None):
        if isinstance(fields, str):
            fields = [fields]
        if chunk_id not in self._data["content"]:
            return
        chunk = self._data["content"][chunk_id]
        for field in fields:
            del chunk[field]
        if len(chunk) == 0:
            self.delete_chunk(chunk_id)


    def serializable(self, include_access_tag=False):
        """
        Returns a dict representing the data in the library that is suitable for
        being serialized e.g. into JSON.
        """
        result = copy.deepcopy(self._data)
        for _, chunk in result['content'].items():
            if not include_access_tag and 'access_tag' in chunk:
                del chunk['access_tag']
            if 'embedding' not in chunk:
                continue
            chunk['embedding'] = base64_from_vector(
                chunk['embedding']).decode('ascii')
        return result


    def save(self, filename):
        result = self.serializable()
        with open(filename, 'w') as f:
            json.dump(result, f, indent='\t')
    

    def similarities(self, query_embedding):
        items = sorted([
            (vector_similarity(query_embedding, item['embedding']), issue_id)
            for issue_id, item
            in self.chunks], reverse=True)
        return {key: value for value, key in items}


    def query(self, version=None, query_embedding=None, query_embedding_model=None, count=0, count_type='token', sort='similarity', sort_reversed=False, seed=None, omit='embedding', access_token=''):
        # We do our own defaulting so that servers that call us can pass the result
        # of request.get() directly and if it's None, we'll use the default.
        if count_type == None:
            count_type = 'token'
        if sort == None:
            sort = 'similarity'
        if omit == None:
            omit = 'embedding'

        if count == 0:
            raise Exception('count must be greater than 0')

        if version == None or version != CURRENT_VERSION:
            raise Exception(f'version must be set to {CURRENT_VERSION}')

        if query_embedding and query_embedding_model != EMBEDDINGS_MODEL_ID:
            raise Exception(
                f'If query_embedding is passed, query_embedding_model must be {EMBEDDINGS_MODEL_ID} but it was {query_embedding_model}')

        if sort not in LEGAL_SORTS:
            raise Exception(
                f'sort {sort} is not one of the legal options: {LEGAL_SORTS}')

        if count_type not in LEGAL_COUNT_TYPES:
            raise Exception(
                f'count_type {count_type} is not one of the legal options: {LEGAL_COUNT_TYPES}')

        result = Library()

        omit_whole_chunk, _, canonical_omit_configuration = keys_to_omit(
            omit)

        result.omit = canonical_omit_configuration

        similarities_dict = None
        if query_embedding:
            # TODO: support query_embedding being base64 encoded or a raw vector of
            # floats
            embedding = vector_from_base64(query_embedding)
            similarities_dict = self.similarities(embedding)

        # The default sort for 'any' or 'similarity' if there was no query set.
        chunk_ids = None 
        if sort == 'similarity' and similarities_dict:
            chunk_ids = list(similarities_dict.keys())
        if sort == 'random':
            chunk_ids = list(self.chunk_ids)
            rng = random.Random()
            rng.seed(None if not seed else seed)
            rng.shuffle(chunk_ids)
            result.omit = 'embedding,similarity'

        if not chunk_ids:
            raise Exception('Invalid type of sort was specified')

        if sort_reversed:
            chunk_ids.reverse()

        count_type_is_chunk = count_type == 'chunk'

        chunk_dict = get_context(chunk_ids, self, count,
                                count_type_is_chunk=count_type_is_chunk)

        visible_access_tags, include_restricted_count, restricted_message = permitted_access(access_token)

        chunk_count = 0
        restricted_count = 0

        for chunk_id, chunk_text in chunk_dict.items():
            chunk = copy.deepcopy(self.chunk(chunk_id))
            if 'access_tag' in chunk:
                if chunk['access_tag'] not in visible_access_tags:
                    restricted_count += 1
                    continue

            chunk_count += 1

            if omit_whole_chunk:
                continue

            # Note: if the text was truncated then technically the embedding isn't
            # necessarily right anymore. But, like, whatever.
            chunk['text'] = chunk_text
            if similarities_dict:
                # the similarity is float32, but only float64 is JSON serializable
                chunk['similarity'] = float(
                    similarities_dict[chunk_id])
            result.set_chunk(chunk_id, chunk)

        result.count_chunks = chunk_count

        if include_restricted_count:
            result.count_restricted = restricted_count
        
        if restricted_message and restricted_count > 0:
            result.message = 'Restricted results were omitted. ' + restricted_message

        return result


def load_default_libraries(fail_on_empty=False) -> Library:
    files = glob.glob(os.path.join(LIBRARY_DIR, '**/*.json'), recursive=True)
    if len(files):
        return load_multiple_libraries(files)
    if fail_on_empty:
        raise Exception('No libraries were in the default library directory.')
    return Library(filename=SAMPLE_LIBRARIES_FILE)


def load_libraries_in_directory(directory) -> Library:
    files = glob.glob(os.path.join(directory, '**/*.json'), recursive=True)
    return load_multiple_libraries(files)


def load_libraries(file=None, fail_on_empty=False) -> Library:
    if file:
        return Library(filename=file)
    return load_default_libraries(fail_on_empty)


def load_multiple_libraries(library_file_names) -> Library:
    result = Library()
    for file in library_file_names:
        library = Library(filename =file)
        result.extend(library)
    return result


def get_token_count(text):
    tokenizer = GPT2TokenizerFast.from_pretrained("gpt2")
    return len(tokenizer.tokenize(text))


def get_context(chunk_ids, library : Library, count=MAX_CONTEXT_LEN_IN_TOKENS, count_type_is_chunk=False):
    """
    Returns a dict of chunk_id to possibly_truncated_chunk_text.

    A count of negative means 'all items'
    """
    result = {}
    context_len = 0
    counter = 0

    # TODO: Account for separator tokens, but do so without invoking a tokenizer in this method.
    for id in chunk_ids:
        if count_type_is_chunk and count >= 0 and counter >= count:
            break
        chunk = library.chunk(id)
        tokens = chunk['token_count']
        text = chunk['text']
        context_len += tokens
        if not count_type_is_chunk and count >= 0 and context_len > count:
            if len(result) == 0:
                result[id] = text[:(count)]
            break
        result[id] = text
        counter += 1
    return result


def get_context_for_library(library : Library):
    """
    Returns an array of all text for every chunk in library
    """
    return [chunk['text'] for (_, chunk) in library.chunks]


def get_chunk_infos_for_library(library : Library):
    """
    Returns all infos for all chunks in library
    """
    infos = []
    if "similarity" in library.fields_to_omit:
        infos = [(_, chunk['info']) for (_, chunk) in library.chunks]
    else:
        infos = [(chunk["similarity"], chunk['info']) for (_, chunk) in library.chunks]
        infos.sort(reverse=True)
    unique_infos = []
    urls = []
    for _, info in infos:
        url = info["url"]
        if url not in urls:
            unique_infos.append(info)
            urls.append(info["url"])
    return unique_infos

LEGAL_SORTS = set(['similarity', 'any', 'random'])
LEGAL_COUNT_TYPES = set(['token', 'chunk'])
LEGAL_OMIT_KEYS = set(
    ['*', '', 'similarity', 'embedding', 'token_count', 'info', 'access_tag'])


def keys_to_omit(configuration=''):
    """
    Takes a configuration, either None, a single string, or a list of strings
    and returns a tuple of (omit_whole_chunk, [keys_to_omit], canonical_configuration).

    If a string is provided, it will be split on ',' to create the list.
    """
    if configuration == None:
        configuration = ''
    if isinstance(configuration, str):
        configuration = configuration.split(',')
    if len(configuration) == 0:
        configuration = ['']
    result = []
    omit_whole_chunk = False
    for item in configuration:
        if item not in LEGAL_OMIT_KEYS:
            raise Exception(f'Illegal omit key type: {item}')
        item = item.lower()
        if item == '':
            if len(configuration) != 1:
                raise Exception("If '' is provided, it must be the only item.")
            continue
        elif item == '*':
            if len(configuration) != 1:
                raise Exception(
                    "If '*' is provided, it must be the only item.")
            omit_whole_chunk = True
            continue
        else:
            result.append(item)
    if len(configuration) == 1:
        configuration = configuration[0]
    return (omit_whole_chunk, set(result), configuration)


def get_completion(prompt):
    response = openai.Completion.create(
        model=COMPLETION_MODEL_NAME,
        prompt=prompt,
        temperature=0.7,
        max_tokens=256,
        top_p=1,
        frequency_penalty=0,
        presence_penalty=0
    )
    return response.choices[0].text.strip()


def get_completion_with_context(query, context):
    # Borrowed from https://github.com/openai/openai-cookbook/blob/838f000935d9df03e75e181cbcea2e306850794b/examples/Question_answering_using_embeddings.ipynb

    prompt = f"Answer the question as truthfully as possible using the provided context, and if the answer is not contained within the text below, say \"I don't know.\"\n\nContext:\n{context} \n\nQuestion:\n{query}\n\nAnswer:"
    return get_completion(prompt)


def ask(query, context_query=None, library_file=None):
    if not context_query:
        context_query = query
    library = load_libraries(library_file)

    query_embedding = get_embedding(context_query)
    similiarities_dict = library.similarities(query_embedding)
    context_dict = get_context(similiarities_dict.keys(), library)

    context = list(context_dict.values())
    chunk_ids = list(context_dict.keys())

    infos = [library.chunk(chunk_id)['info'] for chunk_id in chunk_ids]
    return get_completion_with_context(query, context), infos
