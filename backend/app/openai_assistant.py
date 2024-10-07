import asyncio
import httpx
import re
import string
from signal import SIGINT, SIGTERM

from starlette.websockets import WebSocketDisconnect, WebSocketState
from deepgram import (
    DeepgramClient, DeepgramClientOptions, LiveTranscriptionEvents, LiveOptions
)
import logging
from openai import AsyncOpenAI
from app.config import settings

logger = logging.getLogger("uvicorn")

DEEPGRAM_TTS_URL = 'https://api.deepgram.com/v1/speak?model=aura-luna-en'
SYSTEM_PROMPT = """You are a helpful and enthusiastic assistant. Speak in a human, conversational tone.
Keep your answers as short and concise as possible, like in a conversation, ideally no more than 120 characters.
"""

deepgram_config = DeepgramClientOptions(options={'keepalive': 'true'})
deepgram = DeepgramClient(settings.DEEPGRAM_API_KEY, config=deepgram_config)
dg_connection_options = LiveOptions(
    model='nova-2',
    language='en-US',
    smart_format=True,
    interim_results=True,
    utterance_end_ms='1000',
    vad_events=True,
    endpointing=500,
)
# groq = AsyncGroq(api_key=settings.GROQ_API_KEY)
openai = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

class Assistant:
    def __init__(self, websocket, memory_size=10):
        self.websocket = websocket
        self.transcript_parts = []
        self.transcript_queue = asyncio.Queue()
        self.system_message = {'role': 'system', 'content': SYSTEM_PROMPT}
        self.chat_messages = []
        self.memory_size = memory_size
        self.httpx_client = httpx.AsyncClient()
        self.finish_event = asyncio.Event()

    async def assistant_chat(self, messages, model='gpt-4o-mini', assistant_id='asst_JlpZ8gVj7jkzujLsY3s5yhOr'):
        thread = await openai.beta.threads.create(messages=messages[1:-2])
        await openai.beta.threads.messages.create(
            thread_id=thread.id,
            role="user",
            content=messages[-1]["content"],
        )
        run = await openai.beta.threads.runs.create_and_poll(
            thread_id=thread.id,
            assistant_id=assistant_id,
            )
        if run.status == 'completed': 
            response = await openai.beta.threads.messages.list(
                thread_id=thread.id
            )
            logger.info(f"Assistant response: {response.data[0].content[0].text}")
        else:
            logger.info(f"Assistant response: No response. Run status: {run.status}")
        return response.data[0].content[0].text.value
    
    def should_end_conversation(self, text):
        text = text.translate(str.maketrans('', '', string.punctuation))
        text = text.strip().lower()
        return re.search(r'\b(goodbye|bye)\b$', text) is not None
    
    async def text_to_speech(self, text):
        headers = {
            'Authorization': f'Token {settings.DEEPGRAM_API_KEY}',
            'Content-Type': 'application/json'
        }
        async with self.httpx_client.stream(
            'POST', DEEPGRAM_TTS_URL, headers=headers, json={'text': text}
        ) as res:
            async for chunk in res.aiter_bytes(1024):
                await self.websocket.send_bytes(chunk)
    
    async def transcribe_audio(self):
        self.loop = asyncio.get_event_loop()
        for signal in (SIGTERM, SIGINT):
            self.loop.add_signal_handler(
                signal,
                lambda: asyncio.create_task(self.shutdown(signal, self.loop, dg_connection)),
            )
        dg_connection = deepgram.listen.asynclive.v("1")
        async def on_open(self, open, **kwargs):
            logger.info(f"\nOn Open\n")
        async def on_message(self_handler, result, **kwargs):
            logger.info(f"\nOn Message\n")
            sentence = result.channel.alternatives[0].transcript
            logger.info(f'Transcript: {sentence}')
            if len(sentence) == 0:
                return
            if result.is_final:
                self.transcript_parts.append(sentence)
                await self.transcript_queue.put({'type': 'transcript_final', 'content': sentence})
                if result.speech_final:
                    full_transcript = ' '.join(self.transcript_parts)
                    self.transcript_parts = []
                    await self.transcript_queue.put({'type': 'speech_final', 'content': full_transcript})
            else:
                await self.transcript_queue.put({'type': 'transcript_interim', 'content': sentence})
        
        async def on_metadata(self, metadata, **kwargs):
            logger.info(f"\nOn Metadata\n")

        async def on_speech_started(self, speech_started, **kwargs):
            logger.info(f"\nOn Speech Started\n")

        async def on_utterance_end(self_handler, utterance_end, **kwargs):
            logger.info(f"\nOn Utterance End\n")
            if len(self.transcript_parts) > 0:
                full_transcript = ' '.join(self.transcript_parts)
                self.transcript_parts = []
                await self.transcript_queue.put({'type': 'speech_final', 'content': full_transcript})
        async def on_close(self, close, **kwargs):
            logger.info(f"\nOn Close\n{close}\n\n")

        async def on_error(self, error, **kwargs):
            logger.info(f"\nOn Error\n{error}\n\n")

        async def on_unhandled(self, unhandled, **kwargs):
            logger.info(f"\nOn Unhandled\n{unhandled}\n\n")

        dg_connection.on(LiveTranscriptionEvents.Open, on_open)
        dg_connection.on(LiveTranscriptionEvents.Transcript, on_message)
        dg_connection.on(LiveTranscriptionEvents.Metadata, on_metadata)
        dg_connection.on(LiveTranscriptionEvents.SpeechStarted, on_speech_started)
        dg_connection.on(LiveTranscriptionEvents.UtteranceEnd, on_utterance_end)
        dg_connection.on(LiveTranscriptionEvents.Close, on_close)
        dg_connection.on(LiveTranscriptionEvents.Error, on_error)
        dg_connection.on(LiveTranscriptionEvents.Unhandled, on_unhandled)

        logger.info('Connecting to Deepgram...')
        if await dg_connection.start(dg_connection_options) is False:
            raise Exception('Failed to connect to Deepgram')
        
        try:
            while not self.finish_event.is_set():
                # Receive audio stream from the client and send it to Deepgram to transcribe it
                logger.info('Assistant Transcribing...')
                data = await self.websocket.receive_bytes()
                if isinstance(data, bytes):
                    await dg_connection.send(data)
                else:
                    logger.info(f"Received non-bytes data: {type(data)} {data}")
        finally:
            await dg_connection.finish()
            logger.info('Deepgram connection closed')
    
    async def manage_conversation(self):
        while not self.finish_event.is_set():
            try:
                logger.info('Waiting for transcript...')
                transcript = await self.transcript_queue.get()
                
                if self.websocket.client_state == WebSocketState.DISCONNECTED:
                    logger.info("WebSocket disconnected, ending conversation")
                    self.finish_event.set()
                    break

                if transcript['type'] == 'speech_final':
                    if self.should_end_conversation(transcript['content']):
                        self.finish_event.set()
                        try:
                            await self.websocket.send_json({'type': 'finish'})
                        except Exception as e:
                            logger.error(f"Error sending finish message: {e}")
                        break

                    self.chat_messages.append({'role': 'user', 'content': transcript['content']})
                    response = await self.assistant_chat(
                        [self.system_message] + self.chat_messages[-self.memory_size:]
                    )
                    self.chat_messages.append({'role': 'assistant', 'content': response})
                    await self.websocket.send_json({'type': 'assistant', 'content': response})
                    await self.text_to_speech(response)
                else:
                    await self.websocket.send_json(transcript)
            except Exception as e:
                logger.error(f"Unexpected error in manage_conversation: {e}")
                self.finish_event.set()
                break
    async def shutdown(self, signal, loop, dg_connection):
        logger.info(f"Received exit signal {signal.name}...")
        await dg_connection.finish()
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        [task.cancel() for task in tasks]
        logger.info(f"Cancelling {len(tasks)} outstanding tasks")
        await asyncio.gather(*tasks, return_exceptions=True)
        loop.stop()
        logger.info("Shutdown complete.")
    
    async def run(self):
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.transcribe_audio())
                tg.create_task(self.manage_conversation())
        except* asyncio.CancelledError:
            logger.error("Tasks cancelled")
        except* Exception as e:
            logger.error(f"Unexpected error in run: {e}")
        finally:
            self.finish_event.set()
            await self.httpx_client.aclose()
            if self.websocket.client_state != WebSocketState.DISCONNECTED:
                await self.websocket.close()
            logger.info("Assistant run completed")
