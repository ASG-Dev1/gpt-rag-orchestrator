# imports
import json
import logging
import os
import semantic_kernel as sk
import time
from orc.plugins.Conversation.BingSearch import BingConnector
from orc.plugins.Conversation.Triage.wrapper import triage
from orc.plugins.ResponsibleAI.Fairness.wrapper import fairness
from semantic_kernel.functions.kernel_arguments import KernelArguments
from shared.util import call_semantic_function, get_chat_history_as_messages, get_message, get_last_messages
from shared.util import get_blocked_list, create_kernel, get_usage_tokens, escape_xml_characters
from azure.cosmos.aio import CosmosClient # Fetch Chat History JAMR

# logging level
logging.getLogger('azure').setLevel(logging.WARNING)
LOGLEVEL = os.environ.get('LOGLEVEL', 'debug').upper()
logging.basicConfig(level=LOGLEVEL)
myLogger = logging.getLogger(__name__)

# Env Variables
BLOCKED_LIST_CHECK = os.environ.get("BLOCKED_LIST_CHECK") or "true"
BLOCKED_LIST_CHECK = True if BLOCKED_LIST_CHECK.lower() == "true" else False
GROUNDEDNESS_CHECK = os.environ.get("GROUNDEDNESS_CHECK") or "true"
GROUNDEDNESS_CHECK = True if GROUNDEDNESS_CHECK.lower() == "true" else False
RESPONSIBLE_AI_CHECK = os.environ.get("RESPONSIBLE_AI_CHECK") or "true"
RESPONSIBLE_AI_CHECK = True if RESPONSIBLE_AI_CHECK.lower() == "true" else False
CONVERSATION_METADATA = os.environ.get("CONVERSATION_METADATA") or "true"
CONVERSATION_METADATA = True if CONVERSATION_METADATA.lower() == "true" else False

AZURE_OPENAI_CHATGPT_MODEL = os.environ.get("AZURE_OPENAI_CHATGPT_MODEL")
AZURE_OPENAI_TEMPERATURE = os.environ.get("AZURE_OPENAI_TEMPERATURE") or "0.17"
AZURE_OPENAI_TEMPERATURE = float(AZURE_OPENAI_TEMPERATURE)
AZURE_OPENAI_TOP_P = os.environ.get("AZURE_OPENAI_TOP_P") or "0.27"
AZURE_OPENAI_TOP_P = float(AZURE_OPENAI_TOP_P)
AZURE_OPENAI_RESP_MAX_TOKENS = os.environ.get("AZURE_OPENAI_MAX_TOKENS") or "1000"
AZURE_OPENAI_RESP_MAX_TOKENS = int(AZURE_OPENAI_RESP_MAX_TOKENS)
CONVERSATION_MAX_HISTORY = os.environ.get("CONVERSATION_MAX_HISTORY") or "3"
CONVERSATION_MAX_HISTORY = int(CONVERSATION_MAX_HISTORY)

ORCHESTRATOR_FOLDER = "orc"
PLUGINS_FOLDER = f"{ORCHESTRATOR_FOLDER}/plugins"
BOT_DESCRIPTION_FILE = f"{ORCHESTRATOR_FOLDER}/bot_description.prompt"

async def get_answer(history, client_principal_id):  # Added client_principal_id

    print("JOSHUA TEST")
    print(history)
    print(client_principal_id)

    
    #############################
    # INITIALIZATION
    #############################

    # Initialize variables
    answer_dict = {}
    prompt = "The prompt is only recorded for question-answering intents"
    answer = ""
    intents = []
    bot_description = open(BOT_DESCRIPTION_FILE, "r").read()
    search_query = ""
    sources = []
    bypass_nxt_steps = False  # Flag to bypass unnecessary steps
    blocked_list = []

    # Conversation metadata
    conversation_plugin_answer = ""
    conversation_history_summary = ''
    triage_language = ''
    answer_generated_by = "none"
    prompt_tokens = 0
    completion_tokens = 0

    # Get user question
    messages = get_chat_history_as_messages(history, include_last_turn=True)
    ask = messages[-1]['content']

    logging.info(f"[code_orchest] starting RAG flow. {ask[:50]}")
    init_time = time.time()

    #############################
    # GUARDRAILS (QUESTION)
    #############################

    if BLOCKED_LIST_CHECK:
        logging.debug(f"[code_orchest] blocked list check.")
        try:
            blocked_list = get_blocked_list()
            for blocked_word in blocked_list:
                if blocked_word in ask.lower().split():
                    logging.info(f"[code_orchest] blocked word found in question: {blocked_word}.")
                    answer = get_message('BLOCKED_ANSWER')
                    answer_generated_by = 'blocked_list_check'
                    bypass_nxt_steps = True
                    break
        except Exception as e:
            logging.error(f"[code_orchest] could not get blocked list. {e}")
        response_time = round(time.time() - init_time, 2)
        logging.info(f"[code_orchest] finished blocked list check. {response_time} seconds.")

    #############################
    # RAG-FLOW
    #############################

    if not bypass_nxt_steps:

        try:

            # Create kernel
            kernel = create_kernel()

            # Create the arguments that will be used by semantic functions
            arguments = KernelArguments()
            arguments["bot_description"] = bot_description
            arguments["ask"] = ask
            arguments["history"] = json.dumps(get_last_messages(messages, CONVERSATION_MAX_HISTORY), ensure_ascii=False)
            arguments["previous_answer"] = messages[-2]['content'] if len(messages) > 1 else ""

            # Import RAG plugins
            conversationPlugin = kernel.import_plugin_from_prompt_directory(PLUGINS_FOLDER, "Conversation")
            retrievalPlugin = kernel.import_native_plugin_from_directory(PLUGINS_FOLDER, "Retrieval")

            # Conversation summary
            logging.debug(f"[code_orchest] summarizing conversation")
            start_time = time.time()
            if arguments["history"] != '[]':
                function_result = await call_semantic_function(kernel, conversationPlugin["ConversationSummary"], arguments)
                prompt_tokens += get_usage_tokens(function_result, 'prompt')
                completion_tokens += get_usage_tokens(function_result, 'completion')
                conversation_history_summary = str(function_result)
            else:
                conversation_history_summary = ""
                logging.info(f"[code_orchest] first time talking, no need to summarize.")
            arguments["conversation_summary"] = conversation_history_summary
            response_time = round(time.time() - start_time, 2)
            logging.info(f"[code_orchest] finished summarizing conversation: {conversation_history_summary}. {response_time} seconds.")

            # Triage (find intent and generate answer and search query when applicable)
            logging.debug(f"[code_orchest] checking intent. ask: {ask}")
            start_time = time.time()
            triage_dict = await triage(kernel, conversationPlugin, arguments)
            intents = triage_dict['intents']
            triage_language = triage_dict['language']
            arguments["language"] = triage_language
            prompt_tokens += triage_dict["prompt_tokens"]
            completion_tokens += triage_dict["completion_tokens"]
            response_time = round(time.time() - start_time, 2)
            logging.info(f"[code_orchest] finished checking intents: {intents}. {response_time} seconds.")

            # Handle general intents
            if set(intents).intersection({"about_bot", "off_topic"}):
                answer = triage_dict['answer']
                answer_generated_by = "conversation_plugin_triage"
                logging.info(f"[code_orchest] triage answer: {answer}")

            # Handle question answering intent
            elif set(intents).intersection({"follow_up", "question_answering"}):

                search_query = triage_dict['search_query'] if triage_dict['search_query'] != '' else ask

                # Run retrieval function
                function_result = await kernel.invoke(
                    retrievalPlugin["VectorIndexRetrieval"],
                    sk.KernelArguments(input=search_query, client_principal_id=client_principal_id)  # Added client_principal_id
                )
                sources = function_result.value  # 'sources' is a list

                # For logging purposes, convert 'sources' to a JSON string
                formatted_sources = json.dumps(sources)[:100].replace('\n', ' ')
                logging.info(f"[code_orchest] generating bot answer. sources: {formatted_sources}")

                # Escape XML characters if necessary
                escaped_sources = escape_xml_characters(json.dumps(sources))

                # Assign to arguments; downstream functions expect a string
                arguments["sources"] = escaped_sources

                # Generate the answer augmented by the retrieval
                logging.info(f"[code_orchest] generating bot answer. ask: {ask}")
                start_time = time.time()
                arguments["history"] = json.dumps(messages[:-1], ensure_ascii=False)  # Update context with full history
                function_result = await call_semantic_function(kernel, conversationPlugin["Answer"], arguments)
                answer = str(function_result)
                conversation_plugin_answer = answer
                answer_generated_by = "conversation_plugin_answer"
                prompt_tokens += get_usage_tokens(function_result, 'prompt')
                completion_tokens += get_usage_tokens(function_result, 'completion')
                prompt = str(function_result.metadata['messages'][0])
                response_time = round(time.time() - start_time, 2)
                logging.info(f"[code_orchest] finished generating bot answer. {response_time} seconds. {answer[:100]}.")

            elif "greeting" in intents:
                answer = triage_dict['answer']
                answer_generated_by = "conversation_plugin_triage"
                logging.info(f"[code_orchest] triage answer: {answer}")

            elif intents == ["none"]:
                logging.info(f"[code_orchest] No intent found, review Triage function.")
                answer = get_message('NO_INTENT_ANSWER')
                answer_generated_by = "no_intent_found_check"
                bypass_nxt_steps = True

        except Exception as e:
            logging.error(f"[code_orchest] exception when executing RAG flow. {e}")
            answer = f"{get_message('ERROR_ANSWER')} RAG flow: exception: {e}"
            answer_generated_by = "exception_rag_flow"
            bypass_nxt_steps = True

    #############################
    # GUARDRAILS (ANSWER)
    #############################

    # (Optional) Implement any additional guardrails for the answer here

    #############################
    # BUILD THE RESPONSE
    #############################

    answer_dict["user_ask"] = ask
    answer_dict["answer"] = answer
    answer_dict["search_query"] = search_query

    # Additional metadata for debugging
    if CONVERSATION_METADATA:
        answer_dict["intents"] = intents
        answer_dict["triage_language"] = triage_language
        answer_dict["answer_generated_by"] = answer_generated_by
        answer_dict["conversation_history_summary"] = conversation_history_summary
        answer_dict["conversation_plugin_answer"] = conversation_plugin_answer
        answer_dict["model"] = AZURE_OPENAI_CHATGPT_MODEL
        answer_dict["prompt_tokens"] = prompt_tokens
        answer_dict["completion_tokens"] = completion_tokens

    answer_dict["prompt"] = prompt
    answer_dict["sources"] = sources  # Assign the list directly without using 'replace()'

    response_time = round(time.time() - init_time, 2)
    logging.info(f"[code_orchest] finished RAG Flow. {response_time} seconds.")

    return answer_dict

