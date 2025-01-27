import asyncio
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.openai_assistant import Assistant

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

@app.head('/health')
@app.get('/health')
def health_check():
    return 'ok'

@app.websocket('/listen')
async def websocket_listen(websocket: WebSocket):
    await websocket.accept()
    assistant = Assistant(websocket)
    await assistant.run()
