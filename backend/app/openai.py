import asyncio
import httpx
import re
import string
from starlette.websockets import WebSocketDisconnect, WebSocketState
from deepgram import (
    DeepgramClient, DeepgramClientOptions, LiveTranscriptionEvents, LiveOptions
)
from openai import AsyncOpenAI
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
        chat_completion = await openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Hello world"}]
        )
        print(chat_completion)
        return chat_completion.choices[0].message.content


    
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
        async def on_message(self_handler, result, **kwargs):
            sentence = result.channel.alternatives[0].transcript
            print('Transcript in openai:', sentence)
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

        dg_connection = deepgram.listen.asynclive.v('1')
        dg_connection.on(LiveTranscriptionEvents.Transcript, on_message)
        dg_connection.on(LiveTranscriptionEvents.UtteranceEnd, on_utterance_end)
        print('Connecting to Deepgram...')
        if await dg_connection.start(dg_connection_options) is False:
            raise Exception('Failed to connect to Deepgram')
        
        try:
            audio_file = open('audio.webm', 'wb')
            while not self.finish_event.is_set():
                # Receive audio stream from the client and send it to Deepgram to transcribe it
                print('openai Transcribing...')
                data = await self.websocket.receive_bytes()
                await dg_connection.send(data)
        except Exception as e:
            print('Error:', e)
        finally:
            await dg_connection.finish()
            print('Deepgram connection closed here')
    
    async def manage_conversation(self):
        while not self.finish_event.is_set():
            print('Waiting for transcript...')
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
    
    async def run(self):
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.transcribe_audio())
                tg.create_task(self.manage_conversation())
        except* WebSocketDisconnect:
            print('Client disconnected')
        finally:
            await self.httpx_client.aclose()
            if self.websocket.client_state != WebSocketState.DISCONNECTED:
                await self.websocket.close()
