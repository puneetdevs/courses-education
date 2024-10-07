import asyncio
import httpx
import re
import string
import logging
from starlette.websockets import WebSocketDisconnect, WebSocketState
from deepgram import (
    DeepgramClient, DeepgramClientOptions, LiveTranscriptionEvents, LiveOptions
)
from groq import AsyncGroq
from app.config import settings

DEEPGRAM_TTS_URL = 'https://api.deepgram.com/v1/speak?model=aura-luna-en'
SYSTEM_PROMPT = """You are a helpful and enthusiastic assistant. Speak in a human, conversational tone.
Keep your answers as short and concise as possible, like in a conversation, ideally no more than 120 characters.
"""

deepgram_config = DeepgramClientOptions(options={'keepalive': 'true'})
deepgram = DeepgramClient(settings.DEEPGRAM_API_KEY, config=deepgram_config)
dg_connection_options = LiveOptions(
    model='nova-2',
    language='en',
    # Apply smart formatting to the output
    smart_format=True,
    # To get UtteranceEnd, the following must be set:
    interim_results=True,
    utterance_end_ms='1000',
    vad_events=True,
    # Time in milliseconds of silence to wait for before finalizing speech
    endpointing=500,
)
groq = AsyncGroq(api_key=settings.GROQ_API_KEY)

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
        self.keep_alive_task = None
        self.dg_connection = None

    async def assistant_chat(self, messages, model='llama3-8b-8192'):
        res = await groq.chat.completions.create(messages=messages, model=model)
        return res.choices[0].message.content
    
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
        logger = logging.getLogger(__name__)
        try:
            while not self.finish_event.is_set():
                try:
                    data = await asyncio.wait_for(self.websocket.receive_bytes(), timeout=5.0)
                    print("Received {len(data)} bytes of audio data")
                    await self.dg_connection.send(data)
                except asyncio.TimeoutError:
                    print("No data received, sending keep-alive")
                    await self.dg_connection.send(b'')
                except Exception as e:
                    print("Error in transcribe_audio loop: {e}")
                    break
        except Exception as e:
            print("Error in transcribe_audio: {e}")
        finally:
            print("Transcribe audio task finished")
        
        async def on_message(self_handler, result, **kwargs):
            sentence = result.channel.alternatives[0].transcript
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
        
        async def on_utterance_end(self_handler, utterance_end, **kwargs):
            if len(self.transcript_parts) > 0:
                full_transcript = ' '.join(self.transcript_parts)
                self.transcript_parts = []
                await self.transcript_queue.put({'type': 'speech_final', 'content': full_transcript})

        self.dg_connection = deepgram.listen.asynclive.v('1')
        self.dg_connection.on(LiveTranscriptionEvents.Transcript, on_message)
        self.dg_connection.on(LiveTranscriptionEvents.UtteranceEnd, on_utterance_end)
        if await self.dg_connection.start(dg_connection_options) is False:
            raise Exception('Failed to connect to Deepgram')
        
        try:
            while not self.finish_event.is_set():
                # Receive audio stream from the client and send it to Deepgram to transcribe it
                data = await self.websocket.receive_bytes()
                if data:
                    await self.dg_connection.send(data)
                else:
                    # Send an empty buffer to keep the connection alive
                    await self.dg_connection.send(b'')
                await asyncio.sleep(0.1)  # Add a small delay to prevent busy-waiting
        finally:
            await self.dg_connection.finish()

    async def manage_conversation(self):
        while not self.finish_event.is_set():
            transcript = await self.transcript_queue.get()
            if transcript['type'] == 'speech_final':
                if self.should_end_conversation(transcript['content']):
                    self.finish_event.set()
                    await self.websocket.send_json({'type': 'finish'})
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
    
    async def keep_alive(self):
        while not self.finish_event.is_set():
            try:
                await self.websocket.send_json({"type": "ping"})
                await asyncio.sleep(30)  # Send a ping every 30 seconds
            except Exception as e:
                print(f"Keep-alive error: {e}")
                self.finish_event.set()
                break

    async def run(self):
        try:
            self.keep_alive_task = asyncio.create_task(self.keep_alive())
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.transcribe_audio())
                tg.create_task(self.manage_conversation())
                tg.create_task(self.keep_deepgram_alive())
        except* WebSocketDisconnect:
            print('Client disconnected')
        except* Exception as e:
            print(f"Unexpected error: {e}")
        finally:
            if self.keep_alive_task:
                self.keep_alive_task.cancel()
            await self.httpx_client.aclose()
            if self.websocket.client_state != WebSocketState.DISCONNECTED:
                await self.websocket.close()

    async def keep_deepgram_alive(self):
        while not self.finish_event.is_set():
            if self.dg_connection:
                await self.dg_connection.send(b'')  # Send empty buffer
            await asyncio.sleep(5)  # Adjust the interval as needed
