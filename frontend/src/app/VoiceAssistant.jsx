'use client';

import { useState, useReducer, useRef, useLayoutEffect } from 'react';
import Image from 'next/image';
import conversationReducer from './conversationReducer';
import logo from 'public/logo.svg';
import micIcon from 'public/mic.svg';
import micOffIcon from 'public/mic-off.svg';

const initialConversation = { messages: [], finalTranscripts: [], interimTranscript: '' };

function VoiceAssistant() {
  const [conversation, dispatch] = useReducer(conversationReducer, initialConversation);
  const [isRunning, setIsRunning] = useState(false);
  const [isListening, setIsListening] = useState(false);
  const wsRef = useRef(null);
  const mediaRecorderRef = useRef(null);
  const mediaSourceRef = useRef(null);
  const sourceBufferRef = useRef(null);
  const audioElementRef = useRef(null);
  const audioDataRef = useRef([]);
  const messagesEndRef = useRef(null);

  // Automatically scroll to bottom message
  useLayoutEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [conversation]);

  function openWebSocketConnection() {
    const ws_url = process.env.NEXT_PUBLIC_WEBSOCKET_URL || 'ws://localhost:8000/listen';
    wsRef.current = new WebSocket(ws_url);
    wsRef.current.binaryType = 'arraybuffer';

    wsRef.current.onopen = () => {
      console.log('WebSocket connection opened');
    };

    wsRef.current.onmessage = (event) => {
      console.log('Received message from server');
      if (event.data instanceof ArrayBuffer) {
        handleAudioStream(event.data);
      } else {
        handleJsonMessage(event.data);
      }
    };

    wsRef.current.onerror = (error) => {
      console.error('WebSocket error:', error);
    };

    wsRef.current.onclose = (event) => {
      console.log('WebSocket connection closed:', event.code, event.reason);
    };
  }

  function closeWebSocketConnection() {
    if (wsRef.current) {
      wsRef.current.close();
    }
  }

  async function startMicrophone() {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      mediaRecorderRef.current = new MediaRecorder(stream, { mimeType: 'audio/webm;codecs=opus' });
      mediaRecorderRef.current.addEventListener('dataavailable', async e => {
        if (e.data.size > 0 && wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
          const arrayBuffer = await e.data.arrayBuffer();
          wsRef.current.send(arrayBuffer);
          console.log(`Sent ${arrayBuffer.byteLength} bytes of audio data`);
        }
      });
      mediaRecorderRef.current.start(250);
      console.log('Microphone started');
    } catch (err) {
      console.error('Error starting microphone:', err);
    }
  }

  function stopMicrophone() {
    if (mediaRecorderRef.current && mediaRecorderRef.current.stream) {
      mediaRecorderRef.current.stop();
      mediaRecorderRef.current.stream.getTracks().forEach(track => track.stop());
    }
  }

  function startAudioPlayer() {
    // Initialize MediaSource and event listeners
    mediaSourceRef.current = getMediaSource();
    if (!mediaSourceRef.current) {
      return;
    }
    
    mediaSourceRef.current.addEventListener('sourceopen', () => {
      if (!MediaSource.isTypeSupported('audio/mpeg')) return;
      
      sourceBufferRef.current = mediaSourceRef.current.addSourceBuffer('audio/mpeg');
      sourceBufferRef.current.addEventListener('updateend', () => {
        if (audioDataRef.current.length > 0 && !sourceBufferRef.current.updating) {
          sourceBufferRef.current.appendBuffer(audioDataRef.current.shift());
        }
      });
    });

    // Initialize Audio Element
    const audioUrl = URL.createObjectURL(mediaSourceRef.current);
    audioElementRef.current = new Audio(audioUrl);
    var isPlaying = audioElementRef.currentTime > 0 && !audioElementRef.paused && !audioElementRef.ended && audioElementRef.readyState > audioElementRef.HAVE_ENOUGH_DATA;
    if (!isPlaying){
      audioElementRef.current.play();
    }
  }

  function isAudioPlaying() {
    return audioElementRef.current.readyState === HTMLMediaElement.HAVE_ENOUGH_DATA;
  }

  function skipCurrentAudio() {
    audioDataRef.current = [];
    const buffered = sourceBufferRef.current.buffered;
    if (buffered.length > 0) {
      if (sourceBufferRef.current.updating) {
        sourceBufferRef.current.abort();
      }
      audioElementRef.current.currentTime = buffered.end(buffered.length - 1);
    }
  }

  function stopAudioPlayer() {
    if (audioElementRef.current) {
      var isPlaying = audioElementRef.currentTime > 0 && !audioElementRef.paused && !audioElementRef.ended && audioElementRef.readyState > audioElementRef.HAVE_ENOUGH_DATA;
      if (isPlaying){
        audioElementRef.current.pause();
      }
      URL.revokeObjectURL(audioElementRef.current.src);
      audioElementRef.current = null;
    }

    if (mediaSourceRef.current) {
      if (sourceBufferRef.current) {
        mediaSourceRef.current.removeSourceBuffer(sourceBufferRef.current);
        sourceBufferRef.current = null;
      }
      mediaSourceRef.current = null;
    }

    audioDataRef.current = [];
  }

  async function startConversation() {
    dispatch({ type: 'reset' });
    try {
      openWebSocketConnection();
      await startMicrophone();
      startAudioPlayer();
      setIsRunning(true);
      setIsListening(true);
    } catch (err) {
      console.log('Error starting conversation:', err);
      endConversation();
    }
  }

  function endConversation() {
    closeWebSocketConnection();
    stopMicrophone();
    stopAudioPlayer();
    setIsRunning(false);
    setIsListening(false);
  }

  function toggleListening() {
    if (isListening) {
      mediaRecorderRef.current.pause();
    } else {
      mediaRecorderRef.current.resume();
    }
    setIsListening(!isListening);
  }

  const currentTranscript = [...conversation.finalTranscripts, conversation.interimTranscript].join(' ');

  function handleJsonMessage(data) {
    try {
      const message = JSON.parse(data);
      console.log('Received JSON message:', message);

      switch (message.type) {
        case 'transcript_interim':
          dispatch({ type: 'transcript_interim', content: message.content });
          break;
        case 'transcript_final':
          dispatch({ type: 'transcript_final', content: message.content });
          break;
        case 'speech_final':
          // Handle final speech transcript if needed
          break;
        case 'assistant':
          dispatch({ type: 'assistant', content: message.content });
          break;
        case 'finish':
          endConversation();
          break;
        default:
          console.warn('Unknown message type:', message.type);
      }
    } catch (error) {
      console.error('Error parsing JSON message:', error);
    }
  }

  function handleAudioStream(data) {
    if (sourceBufferRef.current && !sourceBufferRef.current.updating) {
      try {
        sourceBufferRef.current.appendBuffer(data);
      } catch (error) {
        console.error('Error appending buffer:', error);
      }
    } else {
      audioDataRef.current.push(data);
    }
  }

  return (
    <div className='w-full max-w-3xl mx-auto px-4 bg-gray-100 min-h-screen'>
      <div className='sticky top-0 bg-white shadow-md'>
        <header className='flex flex-col gap-2 py-6 bg-gradient-to-r from-purple-900 to-purple-700'>
          <a href='https://lifecoach.voagents.ai'>
            <Image src={logo} width={250} alt='logo' className='mx-auto' priority />
          </a>
          <h1 className='font-urbanist text-2xl font-semibold mx-auto text-white'>AI Voice Assistant</h1>
        </header>
        <div className='flex flex-col justify-center items-center py-6 bg-white'>
          <div className='wave-container'>
            <div className={`wave ${isRunning ? 'running' : ''}`} />
          </div>
          <p className='mt-8 text-sm text-purple-700'>
            {isRunning
              ? 'You can also end the conversation by saying "bye" or "goodbye"'
              : 'Click here to start a voice conversation with the assistant'
            }
          </p>
          <div className='flex items-center mt-4 gap-6'>
            <button
              className='w-48 border-2 border-purple-700 text-purple-700 font-semibold px-4 py-2 rounded-full hover:bg-purple-100 transition duration-300'
              onClick={isRunning ? endConversation : startConversation}
            >
              {isRunning ? 'End conversation' : 'Start conversation'}
            </button>
            <button
              className='h-12 w-12 flex justify-center items-center bg-purple-700 rounded-full shadow-lg hover:bg-purple-600 disabled:opacity-50 transition duration-300'
              onClick={toggleListening}
              disabled={!isRunning}
            >
              <Image src={isListening ? micIcon : micOffIcon} height={24} width={24} alt='microphone' className='invert' />
            </button>
          </div>
        </div>
      </div>
      <div className='flex flex-col items-start py-6 rounded-lg space-y-4 mt-4'>
        {conversation.messages.map(({ role, content }, idx) => (
          <div key={idx} className={`${role === 'user' ? 'bg-purple-100 self-end' : 'bg-white'} p-4 rounded-lg shadow-md max-w-[80%]`}>
            {content}
          </div>
        ))}
        {currentTranscript && (
          <div className='bg-purple-100 p-4 rounded-lg shadow-md max-w-[80%] self-end'>{currentTranscript}</div>
        )}
        <div ref={messagesEndRef} />
      </div>
    </div>
  );
}

function getMediaSource() {
  if ('MediaSource' in window) {
    return new MediaSource();
  } else if ('ManagedMediaSource' in window) {
    // Use ManagedMediaSource if available in iPhone
    return new ManagedMediaSource();
  } else {
    console.log('No MediaSource API available');
    return null;
  }
}

export default VoiceAssistant;