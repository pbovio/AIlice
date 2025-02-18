import time
import os
import re
import simplejson as json
import traceback
import queue
from termcolor import colored

import threading
from ailice.common.AConfig import config
from ailice.core.AProcessor import AProcessor
from ailice.core.llm.ALLMPool import llmPool
from ailice.common.utils.ALogger import ALogger
from ailice.common.utils.AFileUtils import serialize
from ailice.common.ARemoteAccessors import clientPool
from ailice.common.AMessenger import messenger
from ailice.AServices import StartServices, TerminateSubprocess

from ailice.common.APrompts import promptsManager
from ailice.prompts.APromptChat import APromptChat
from ailice.prompts.APromptMain import APromptMain
from ailice.prompts.APromptSearchEngine import APromptSearchEngine
from ailice.prompts.APromptResearcher import APromptResearcher
from ailice.prompts.APromptCoder import APromptCoder
from ailice.prompts.APromptModuleCoder import APromptModuleCoder
from ailice.prompts.APromptCoderProxy import APromptCoderProxy
from ailice.prompts.APromptArticleDigest import APromptArticleDigest

import gradio as gr

def mainLoop(modelID: str, quantization: str, maxMemory: dict, prompt: str, temperature: float, flashAttention2: bool, speechOn: bool, ttsDevice: str, sttDevice: str, contextWindowRatio: float, trace: str):
    config.Initialize(modelID = modelID)
    config.quantization = quantization
    config.maxMemory = maxMemory
    config.temperature = temperature
    config.flashAttention2 = flashAttention2
    config.speechOn = speechOn
    config.contextWindowRatio = contextWindowRatio
    
    print(colored("In order to simplify installation and usage, we have set local execution as the default behavior, which means AI has complete control over the local environment. \
To prevent irreversible losses due to potential AI errors, you may consider one of the following two methods: the first one, run AIlice in a virtual machine; the second one, install Docker, \
use the provided Dockerfile to build an image and container, and modify the relevant configurations in config.json. For detailed instructions, please refer to the documentation.", "red"))

    StartServices()
    for i in range(5):
        try:
            clientPool.Init()
            break
        except Exception as e:
            if i == 4:
                print(f"It seems that some peripheral module services failed to start. EXCEPTION: {str(e)}")
                traceback.print_tb(e.__traceback__)
                exit(-1)
            time.sleep(5)
            continue

    print(colored(">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>", "green"))
    print("We now start the vector database. Note that this may include downloading the model weights, so it may take some time.")
    storage = clientPool.GetClient(config.services['storage']['addr'])
    msg = storage.Open("")
    print(f"Vector database has been started. returned msg: {msg}")
    print(colored(">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>", "green"))
    
    if speechOn:
        import sounddevice as sd
        speech = clientPool.GetClient(config.services['speech']['addr'])
        print(colored(">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>", "green"))
        print("The speech module is preparing speech recognition and TTS models, which may include the work of downloading weight data, so it may take a long time.")
        speech.PrepareModel()
        print("The speech module model preparation work is completed.")
        print(colored(">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>", "green"))
        if any([re.fullmatch(r"(cuda|cpu)(:(\d+))?", s) == None for s in [ttsDevice, sttDevice]]):
            print("the value of ttsDevice and sttDevice should be a valid cuda device, such as cuda, cuda:0, or cpu, the default is cpu.")
            exit(-1)
        else:
            speech.SetDevices({"tts": ttsDevice, "stt": sttDevice})
    else:
        speech = None
    
    timestamp = str(time.time())
    collection = "ailice" + timestamp

    promptsManager.Init(storage=storage, collection=collection)
    for promptCls in [APromptChat, APromptMain, APromptSearchEngine, APromptResearcher, APromptCoder, APromptModuleCoder, APromptCoderProxy, APromptArticleDigest]:
        promptsManager.RegisterPrompt(promptCls)
    
    llmPool.Init([modelID])
    
    logger = ALogger(speech=None)
    processor = AProcessor(name="AIlice", modelID=modelID, promptName=prompt, outputCB=logger.Receiver, collection=collection)
    processor.RegisterModules([config.services['browser']['addr'],
                               config.services['arxiv']['addr'],
                               config.services['google']['addr'],
                               config.services['duckduckgo']['addr'],
                               config.services['scripter']['addr'],
                               config.services['computer']['addr']])
    
    audioQue = queue.Queue(maxsize=100)

    def playAudio():
        while True:
            sr, audio = audioQue.get()
            sd.play(audio, sr)
            sd.wait()
    if speechOn:
        threadPlayer = threading.Thread(target=playAudio, args=())
        threadPlayer.start()
    
    def bot(history):
        if "" != trace.strip():
            with open(trace + "/ailice-trace-" + timestamp + ".json", "w") as f:
                json.dump(processor.ToJson(), f, indent=2, default=serialize)
        
        if str != type(history[-1][0]):
            msg = f'Please observe this image. \n![]({history[-1][0][0]})\n'
        else:
            msg = history[-1][0]
        threadLLM = threading.Thread(target=processor, args=(msg,))
        threadLLM.start()
        history[-1][1] = ""
        while True:
            channel, txt, action = logger.queue.get()
            if ">" == channel:
                threadLLM.join()
                return
            history[-1][1] += "\r\r" if "open"==action else ""
            history[-1][1] += txt
            if speechOn:
                audioQue.put(tts(txt))
            yield history

    def interrupt(text):
        messenger.Put(text)
        messenger.Unlock()
        return
    
    def add_text(history, text):
        history = history + [(text, None)]
        return history, gr.Textbox(value="", interactive=False)

    def add_file(history, file):
        history = history + [((file.name,), None)]
        return history
    
    def stt(history, audio):
        text = speech.Speech2Text(audio[1], audio[0])
        history = history + [(text, None)]
        return history
    
    def tts(txt):
        wav, sr = speech.Text2Speech(txt)
        return sr, wav

    CSS ="""
    .contain { display: flex; flex-direction: column; }
    .gradio-container { height: 100vh !important; }
    #component-0 { height: 100%; }
    #chatbot { flex-grow: 1; overflow: auto;}
    """
    with gr.Blocks(css=CSS) as demo:
        chatbot = gr.Chatbot(
            [],
            elem_id="chatbot",
            bubble_full_width=False,
            line_breaks=False,
            avatar_images=(None, (os.path.join(os.path.dirname(__file__), f"../AIlice.png"))),
        )

        with gr.Row():
            txt = gr.Textbox(
                scale=4,
                show_label=False,
                placeholder="Enter text and press enter, or upload an image",
                container=False,
            )
            btn = gr.UploadButton("📁", file_types=["image"])
            if speechOn:
                audio = gr.Audio(sources=["microphone"], type="numpy", editable=False)

        txt.submit(add_text, [chatbot, txt], [chatbot, txt], queue=False).then(bot, chatbot, chatbot, api_name="bot_response"
            ).then(lambda: gr.Textbox(interactive=True), None, [txt], queue=False)
        
        btn.upload(add_file, [chatbot, btn], [chatbot], queue=False).then(bot, chatbot, chatbot)
        
        if speechOn:
            audio.stop_recording(stt, [chatbot, audio], [chatbot], queue=False).then(
                bot, chatbot, chatbot)
            
        with gr.Row():
            interruptTxt = gr.Textbox(
                scale=4,
                show_label=False,
                placeholder="Send an interrupt message to the currently active agent. Used to rescue agent from errors.",
                container=False,
                interactive=False,
                visible=False
            )
            interruptBtn = gr.Button("Interrupt")
        
        interruptBtn.click(lambda: messenger.Lock(), [], []).then(lambda: gr.Textbox(interactive=True, visible=True), [], [interruptTxt]).then(lambda: gr.Button("Interrupt", interactive=False), [], [interruptBtn])
        interruptTxt.submit(interrupt, [interruptTxt], []).then(lambda: gr.Textbox(value="", interactive=False, visible=False), [], [interruptTxt]).then(lambda: gr.Button("Interrupt", interactive=True), [], [interruptBtn])
        
    demo.queue()
    demo.launch()
    return

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--modelID',type=str,default='hf:Open-Orca/Mistral-7B-OpenOrca', help="modelID specifies the model. The currently supported models can be seen in llm/ALLMPool.py, just copy it directly. We will implement a simpler model specification method in the future.")
    parser.add_argument('--quantization',type=str,default='', help="quantization is the quantization option, you can choose 4bit or 8bit. The default is not quantized.")
    parser.add_argument('--maxMemory',type=dict,default=None, help='maxMemory is the memory video memory capacity constraint, the default is not set, the format when set is like "{0:"23GiB", 1:"24GiB", "cpu": "64GiB"}".')
    parser.add_argument('--prompt',type=str,default='main', help="prompt specifies the prompt to be executed, which is the type of agent. The default is 'main', this agent will decide to call the appropriate agent type according to your needs. You can also specify a special type of agent and interact with it directly.")
    parser.add_argument('--temperature',type=float,default=0.0, help="temperature sets the temperature parameter of LLM reasoning, the default is zero.")
    parser.add_argument('--flashAttention2',action='store_true', help="flashAttention2 is the switch to enable flash attention 2 to speed up inference. It may have a certain impact on output quality.")
    parser.add_argument('--contextWindowRatio',type=float,default=0.6, help="contextWindowRatio is a user-specified proportion coefficient, which determines the proportion of the upper limit of the prompt length constructed during inference to the LLM context window in some cases. The default value is 0.6.")
    parser.add_argument('--speechOn',action='store_true', help="speechOn is the switch to enable voice conversation. Please note that the voice dialogue is currently not smooth yet.")
    parser.add_argument('--ttsDevice',type=str,default='cpu',help='ttsDevice specifies the computing device used by the text-to-speech model. The default is "cpu", you can set it to "cuda" if there is enough video memory.')
    parser.add_argument('--sttDevice',type=str,default='cpu',help='sttDevice specifies the computing device used by the speech-to-text model. The default is "cpu", you can set it to "cuda" if there is enough video memory.')
    parser.add_argument('--trace',type=str,default='', help="trace is used to specify the output directory for the execution history data. This option is empty by default, indicating that the execution history recording feature is not enabled.")
    kwargs = vars(parser.parse_args())

    try:
        mainLoop(**kwargs)
    except Exception as e:
        print(f"Encountered an exception, AIlice is exiting: {str(e)}")
        traceback.print_tb(e.__traceback__)
        TerminateSubprocess()
        raise

if __name__ == '__main__':
    main()