import io
import json
import time
import uuid
from queue import Queue
from typing import List

import openai
import tiktoken
from anyio.from_thread import start_blocking_portal
from django.conf import settings
from langchain.schema import AIMessage, HumanMessage
from pinecone import QueryResponse

from ayushma.models import ChatMessage
from ayushma.models.document import Document
from ayushma.models.enums import ChatMessageType
from ayushma.utils.langchain import LangChainHelper
from ayushma.utils.language_helpers import text_to_speech, translate_text
from ayushma.utils.upload_file import upload_file


def get_embedding(
    text: List[str],
    model: str = "text-embedding-ada-002",
    openai_api_key: str = settings.OPENAI_API_KEY,
) -> List[List[float]]:
    """
    Generates embeddings for the given list of texts using the OpenAI API.

    Args:
        text (List[str]): A list of strings to be embedded.
        model (str, optional): The name of the OpenAI model to use for embedding.
            Defaults to "text-embedding-ada-002".

    Returns:
        A list of embeddings generated by the OpenAI API for the input texts.

    Raises:
        OpenAIError: If there was an error communicating with the OpenAI API.

    Example usage:
        >>> get_embedding(["Hello, world!", "How are you?"])
        [[-0.123, 0.456, 0.789, ...], [0.123, -0.456, 0.789, ...]]

    """
    openai.api_key = openai_api_key
    res = openai.Embedding.create(input=text, model=model)
    return [record["embedding"] for record in res["data"]]


def get_sanitized_reference(pinecone_references: List[QueryResponse]) -> str:
    """
    Extracts the text from the Pinecone QueryResponse object and sanitizes it.

    Args:
        pinecone_reference (List[QueryResponse]): The similar documents retrieved from
            the Pinecone index.

    Returns:
        A string containing the document id and text from the Pinecone QueryResponse object.

    Example usage:
        >>> get_sanitized_reference([QueryResponse(...), QueryResponse(...)])
        "{'28': 'Hello how are you, I am fine, thank you.', '21': 'How was your day?, Mine was good.'}"
    """
    sanitized_reference = {}

    for reference in pinecone_references:
        for match in reference.matches:
            try:
                document_id = str(match.metadata["document"])
                text = str(match.metadata["text"]).replace("\n", " ") + ","
                if document_id in sanitized_reference:
                    sanitized_reference[document_id] += text
                else:
                    sanitized_reference[document_id] = text
            except Exception as e:
                print(e)
                pass

    return json.dumps(sanitized_reference)


def num_tokens_from_string(string: str, encoding_name: str) -> int:
    """Returns the number of tokens in a text string."""
    encoding = tiktoken.get_encoding(encoding_name)
    num_tokens = len(encoding.encode(string))
    return num_tokens


def split_text(text):
    """Returns one string split into n equal length strings"""
    n = len(text)
    number_of_chars = 8192
    parts = []

    for i in range(0, n, number_of_chars):
        part = text[i : i + number_of_chars]
        parts.append(part)

    return parts


def create_json_response(input_text, chat_id, delta, message, stop, ayushma_voice):
    json_data = {
        "chat": str(chat_id),
        "input": input_text,
        "delta": delta,
        "message": message,
        "stop": stop,
        "ayushma_voice": ayushma_voice,
    }

    return "data: " + json.dumps(json_data) + "\n\n"


def get_reference(text, openai_key, namespace, top_k):
    num_tokens = num_tokens_from_string(text, "cl100k_base")
    embeddings: List[List[List[float]]] = []
    if num_tokens < 8192:
        try:
            embeddings.append(get_embedding(text=[text], openai_api_key=openai_key))
        except Exception as e:
            return Exception(
                e.__str__(),
            )
    else:
        parts = split_text(text)
        for part in parts:
            try:
                embeddings.append(get_embedding(text=[part], openai_api_key=openai_key))
            except Exception as e:
                raise Exception(
                    e.__str__(),
                )
    # find similar embeddings from pinecone index for each embedding
    pinecone_references: List[QueryResponse] = []

    for embedding in embeddings:
        similar: QueryResponse = settings.PINECONE_INDEX_INSTANCE.query(
            vector=embedding,
            top_k=int(top_k),
            namespace=namespace,
            include_metadata=True,
        )
        pinecone_references.append(similar)
    return get_sanitized_reference(pinecone_references=pinecone_references)


def add_reference_documents(chat_message):
    ref_text = "References:"
    chat_text = str(chat_message.original_message)
    ref_start_idx = chat_text.find(ref_text)
    if ref_start_idx == -1:
        return

    try:
        doc_ids = chat_text[ref_start_idx + len(ref_text) :].split(",")
        doc_ids = [doc_id.strip(" .,[]*'\"") for doc_id in doc_ids]
        doc_ids = set([str(doc_id) for doc_id in doc_ids if doc_id != ""])
        for doc_id in doc_ids:
            try:
                doc = Document.objects.get(external_id=doc_id)
                chat_message.reference_documents.add(doc)
            except Document.DoesNotExist:
                pass
    except Exception as e:
        print("Error adding reference documents: ", e)

    chat_message.original_message = chat_text[
        :ref_start_idx
    ].strip()  # Strip to remove empty line at the end \nRefereces:
    chat_message.save()


def handle_post_response(
    chat_response,
    chat,
    match_number,
    user_language,
    temperature,
    stats,
    language,
    generate_audio=True,
):
    chat_message: ChatMessage = ChatMessage.objects.create(
        original_message=chat_response,
        chat=chat,
        messageType=ChatMessageType.AYUSHMA,
        top_k=match_number,
        temperature=temperature,
        language=language,
    )
    add_reference_documents(chat_message)
    translated_chat_response = chat_message.original_message
    if user_language != "en-IN":
        stats["response_translation_start_time"] = time.time()
        translated_chat_response = translate_text(
            user_language, chat_message.original_message
        )
    stats["response_translation_end_time"] = time.time()

    ayushma_voice = None
    if generate_audio == True:
        stats["tts_start_time"] = time.time()
        ayushma_voice = text_to_speech(translated_chat_response, user_language)
        stats["tts_end_time"] = time.time()

    url = None
    if ayushma_voice:
        stats["upload_start_time"] = time.time()
        url = upload_file(
            file=io.BytesIO(ayushma_voice),
            s3_key=f"{chat.id}_{uuid.uuid4()}.mp3",
        )
        stats["upload_end_time"] = time.time()

    chat_message.message = translated_chat_response
    chat_message.ayushma_audio_url = url
    chat_message.meta = {
        "translate_start": stats.get("response_translation_start_time"),
        "translate_end": stats.get("response_translation_end_time"),
        "reference_start": stats.get("reference_start_time"),
        "reference_end": stats.get("reference_end_time"),
        "response_start": stats.get("response_start_time"),
        "response_end": stats.get("response_end_time"),
        "tts_start": stats.get("tts_start_time"),
        "tts_end": stats.get("tts_end_time"),
        "upload_start": stats.get("upload_start_time"),
        "upload_end": stats.get("upload_end_time"),
    }
    chat_message.save()
    return translated_chat_response, url, chat_message


def converse(
    english_text,
    local_translated_text,
    openai_key,
    chat,
    match_number,
    user_language,
    temperature,
    stats={},
    stream=True,
    references=None,
    generate_audio=True,
):
    if not openai_key:
        raise Exception("OpenAI-Key header is required to create a chat or converse")

    english_text = english_text.replace("\n", " ")
    language = user_language.split("-")[0]
    nurse_query = ChatMessage.objects.create(
        message=local_translated_text,
        original_message=english_text,
        chat=chat,
        messageType=ChatMessageType.USER,
        language=language,
        meta={
            "translate_start": stats.get("request_translation_start_time"),
            "translate_end": stats.get("request_translation_end_time"),
        },
    )

    stats["reference_start_time"] = time.time()

    if references:
        reference = references
    elif chat.project and chat.project.external_id:
        reference = get_reference(
            english_text, openai_key, str(chat.project.external_id), match_number
        )
    else:
        reference = ""

    stats["reference_end_time"] = time.time()

    stats["response_start_time"] = time.time()

    prompt = chat.prompt or (chat.project and chat.project.prompt)

    # excluding the latest query since it is not a history
    previous_messages = (
        ChatMessage.objects.filter(chat=chat)
        .exclude(id=nurse_query.id)
        .order_by("created_at")
    )
    chat_history = []
    for message in previous_messages:
        if message.messageType == ChatMessageType.USER:
            chat_history.append(HumanMessage(content=f"Nurse: {message.message}"))
        elif message.messageType == ChatMessageType.AYUSHMA:
            chat_history.append(AIMessage(content=f"Ayushma: {message.message}"))

    if stream == False:
        lang_chain_helper = LangChainHelper(
            stream=False,
            openai_api_key=openai_key,
            prompt_template=prompt,
            temperature=temperature,
        )
        response = lang_chain_helper.get_response(english_text, reference, chat_history)
        chat_response = response.replace("Ayushma: ", "")
        stats["response_end_time"] = time.time()
        translated_chat_response, url, chat_message = handle_post_response(
            chat_response,
            chat,
            match_number,
            user_language,
            temperature,
            stats,
            language,
            generate_audio,
        )

        yield chat_message

    else:
        token_queue = Queue()
        RESPONSE_END = object()

        lang_chain_helper = LangChainHelper(
            stream=stream,
            token_queue=token_queue,
            end=RESPONSE_END,
            openai_api_key=openai_key,
            prompt_template=prompt,
            temperature=temperature,
        )

        with start_blocking_portal() as portal:
            portal.start_task_soon(
                lang_chain_helper.get_aresponse,
                RESPONSE_END,
                token_queue,
                english_text,
                reference,
                chat_history,
            )
            chat_response = ""
            skip_token = len("Ayushma: ")
            try:
                while True:
                    if token_queue.empty():
                        continue
                    next_token = token_queue.get(True, timeout=10)
                    if skip_token > 0:
                        skip_token -= 1
                        continue
                    if next_token is RESPONSE_END:
                        stats["response_end_time"] = time.time()
                        (
                            translated_chat_response,
                            url,
                            chat_message,
                        ) = handle_post_response(
                            chat_response,
                            chat,
                            match_number,
                            user_language,
                            temperature,
                            stats,
                            language,
                            generate_audio,
                        )

                        yield create_json_response(
                            local_translated_text,
                            chat.external_id,
                            "",
                            translated_chat_response,
                            True,
                            ayushma_voice=url,
                        )
                        break
                    chat_response += next_token
                    yield create_json_response(
                        local_translated_text,
                        chat.external_id,
                        next_token,
                        chat_response,
                        False,
                        None,
                    )
            except Exception as e:
                print(e)
                error_text = str(e)
                translated_error_text = error_text
                if user_language != "en-IN":
                    translated_error_text = translate_text(user_language, error_text)

                ChatMessage.objects.create(
                    message=translated_error_text,
                    original_message=error_text,
                    chat=chat,
                    messageType=ChatMessageType.AYUSHMA,
                    language=language,
                    meta={
                        "translate_start": stats.get("response_translation_start_time"),
                        "translate_end": stats.get("response_translation_end_time"),
                        "reference_start": stats.get("reference_start_time"),
                        "reference_end": stats.get("reference_end_time"),
                        "response_start": stats.get("response_start_time"),
                        "response_end": stats.get("response_end_time"),
                        "tts_start": stats.get("tts_start_time"),
                        "tts_end": stats.get("tts_end_time"),
                        "upload_start": stats.get("upload_start_time"),
                        "upload_end": stats.get("upload_end_time"),
                    },
                )
                yield create_json_response(
                    local_translated_text, chat.external_id, "", str(e), True, None
                )
