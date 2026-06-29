import discord
from discord.ext import commands
import asyncio
import random
import os
import re
import aiohttp
import time
import json
from collections import deque
from datetime import datetime
from groq import Groq

# ========== CONFIGURATION ==========
TOKEN = os.getenv("TOKEN") # Your main Discord user token
if not TOKEN:
    print(" TOKEN environment variable not set.")
    exit(1)
GROQ_API_KEY = "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" # groq api key

# ========== GLOBAL REGISTRY ==========
hosted_bots = []
hosted_bots_set = set()

# ========== LOAD / SAVE HELPERS ==========
async def load_lines_async(file_path):
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip()]
    except:
        return []

def load_lines(file_path):
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip()]
    except:
        return []

def load_proxies():
    path = "proxies.txt"
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        proxies = [line.strip() for line in f if line.strip()]
    return proxies

def get_random_proxy():
    proxies = load_proxies()
    if not proxies:
        return None
    return random.choice(proxies)

def save_wordlist(name, lines):
    with open(f"wordlist_{name}.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

def extract_target_channel(content, guild):
    if not guild:
        return None
    
    match = re.search(r'(?:reply|tell|say|answer|send)\s+in\s+<#(\d+)>', content, re.IGNORECASE)
    if match:
        channel_id = int(match.group(1))
        return guild.get_channel(channel_id)
    
    match = re.search(r'(?:reply|say|tell|answer|send)\s+in\s+(\d+)', content, re.IGNORECASE)
    if match:
        channel_id = int(match.group(1))
        return guild.get_channel(channel_id)
    
    match = re.search(r'(?:reply|say|tell|answer|send)\s+in\s+#(\S+)', content, re.IGNORECASE)
    if match:
        channel_name = match.group(1).lower()
        for channel in guild.channels:
            if channel.name.lower() == channel_name:
                return channel
    
    match = re.search(r'(?:reply|say|tell|answer|send)\s+in\s+(.+?)(?:\s+[\w]+|$)', content, re.IGNORECASE)
    if match:
        channel_name = match.group(1).strip().lower()
        for channel in guild.channels:
            if channel.name.lower() == channel_name:
                return channel
        
        normalized_query = channel_name.replace(' ', '-').replace('_', '-')
        for channel in guild.channels:
            normalized_name = channel.name.lower().replace(' ', '-').replace('_', '-')
            if normalized_query == normalized_name:
                return channel
        
        query_words = set(channel_name.split())
        for channel in guild.channels:
            name_words = set(channel.name.lower().split())
            if query_words.issubset(name_words):
                return channel
    
    return None

# ========== GROQ CLIENTS ==========
groq = Groq(api_key=GROQ_API_KEY)

# ========== ANTI AFK LOGIC ==========
EXTRACT_PROMPT = """Extract the secret answer from the message. Return ONLY the exact word/phrase, nothing else – no explanation, no extra words, no punctuation.
Examples:
- "afk check say pineapple" → pineapple
- "kw = strawberry" → strawberry
- "reply with hello" → hello
- "type: apple" → apple
- "||hidden|| answer is watermelon" → watermelon
- "**bold** keyword: orange" → orange
- "verify: cat" → cat
- "what is the secret word? banana" → banana
- "what is capital of russia? → moscow
- "what is date today?" → 26 jan
- "tell what is formula of Sodium Chloride" → NaCl
- "what is fastest animal?" → cheetah
- "what is 1+1*1/1+1?" → 1
- "kw = anti + add the string value which is "hello"" → anti hello
Return ONLY the answer word/phrase, nothing else."""

NUMBERS_PATTERN = re.compile(r'^(\d+\s+)+\d+$')
ROMAN_PATTERN = re.compile(r'^([IVXLCDMivxlcdm]+\s+)+[IVXLCDMivxlcdm]+$')
WORD_NUM_PATTERN = re.compile(r'^([A-Za-z]+\d+\s+)+[A-Za-z]+\d+$')
ENGLISH_NUMBERS = r'(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)'
ENGLISH_PATTERN = re.compile(r'^(' + ENGLISH_NUMBERS + r'\s+)+' + ENGLISH_NUMBERS + r'$', re.IGNORECASE)

def roman_to_int(roman):
    roman = roman.upper().strip()
    values = {'I':1,'V':5,'X':10,'L':50,'C':100,'D':500,'M':1000}
    total = 0
    prev = 0
    for ch in reversed(roman):
        val = values.get(ch)
        if not val: return None
        if val < prev: total -= val
        else: total += val
        prev = val
    return total if total <= 20 else None

def word_to_int(word):
    words = {"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9,"ten":10,
             "eleven":11,"twelve":12,"thirteen":13,"fourteen":14,"fifteen":15,"sixteen":16,"seventeen":17,
             "eighteen":18,"nineteen":19,"twenty":20}
    return words.get(word.lower())

def parse_count_number(text):
    text = text.strip()
    if text.isdigit(): return int(text)
    r = roman_to_int(text)
    if r is not None: return r
    return word_to_int(text)

def is_counting_message(text):
    text = text.strip()
    if not text: return False
    return (bool(NUMBERS_PATTERN.match(text)) or bool(ROMAN_PATTERN.match(text)) or
            bool(WORD_NUM_PATTERN.match(text)) or bool(ENGLISH_PATTERN.match(text)))

def simple_keyword_extract(text):
    patterns = [r'kw\s*=\s*(\w+)', r'say\s+(\w+)', r'reply with\s+(\w+)', r'type:\s*(\w+)',
                r'answer is\s+(\w+)', r'keyword:?\s*(\w+)', r'capital of\s+(\w+)', r'formula of\s+(\w+)']
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m: return m.group(1).strip()
    return None

def extract_answer(message_text):
    try:
        resp = groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": EXTRACT_PROMPT},
                      {"role": "user", "content": message_text}],
            temperature=0.0, max_tokens=50
        )
        ans = resp.choices[0].message.content.strip()
        if ans == "NONE" or not ans:
            return simple_keyword_extract(message_text)
        return ans
    except:
        return simple_keyword_extract(message_text)

# ========== AI PACK GENERATOR ==========
def generate_ai_pack(pack_type: str, pack_size: int) -> str:
    system_prompt = """You are a Discord pack generator that creates swear and messages containing abuse for virtual "pack openings". 
The theme can be anything: rare, epic, joke, meme, roast (non‑personal), silly, etc. 
Keep the message to 4-5 number of sentences, use humour, puns, or absurdity.
your motive is to roast and humiliate the other person badly.
often include abuses like "bitch, whore, trash, dork, cunt, moron, fucking, shit, loser, pedophile, shitty, ass, fuck, slut, cuck, maggot" dont include them in every line.
dont add exclamation signs or fullstops or commas.
keep starting words,middle words, and last words unique in every pack.
dont add the words "just" and "pack" in the pack.
sentence should only be one.
Respond with only the pack message, no extra text."""
    user_prompt = f"Create a {pack_type} pack for roasting opening with {pack_size} items."
    try:
        resp = groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_prompt}],
            temperature=0.9, max_tokens=100
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[Pack Generator Error] {e}")
        return "error"

# ========== SUPREME TOOL CLASS ==========
class SupremeBot(commands.Bot):
    def __init__(self, token, command_prefix=".", **options):
        super().__init__(command_prefix=command_prefix, self_bot=True, **options)
        self.token = token
        self.current_page = 1  
        
        # Per-bot state
        self.tasks = {}
        self.spam_tasks = []
        self.reaction_emojis = []
        self.auto_reply_tasks = {}
        self.ar_replied_ids = {}
        self.wordlists = {}
        self.BEEF_WORDS = []
        self.aball_tasks = {}
        self.react_tasks = {}
        self.mimic_tasks = {}
        self.mimic_enabled = False
        self.spamall_tasks = {}
        self.pending_import = {}
        self.deleted_cache = {}
        self.snipe_enabled = set()
        self.start_time = time.time()
        self.name_task = None
        self.afk_task = None
        self.stream_task = None
        self.gc_task = None
        self.anti_target_channel = None
        self.anti_user_history = {}
        self.anti_user_last_number = {}
        self.count_tasks = {}
        self.autopaste_msgs = {}
        self.stam_msgs = {}
        self.persistent_spam_tasks = {}
        self.PERSISTENT_SPAM_FILE = "spam_state.json"
        self.token_pool = []
        self.multireact_tasks = {}
        self.multistam_tasks = {}
        self.multicount_tasks = {}
        
    async def setup_hook(self):
        """Called automatically when the bot is ready to set up commands"""
        self._register_events()
        self._register_commands()

    def _register_events(self):
        @self.event
        async def on_ready():
            print(f" Logged in as: {self.user} (ID: {self.user.id})")
            print(f" Prefix: {self.command_prefix}")
            print("Type .menu to see all commands")
        
        @self.event
        async def on_message(message):

            # Store message in cache for snipe
            self.deleted_cache[message.id] = {
                "content": message.content,
                "author": f"{message.author} ({message.author.id})",
                "time": message.created_at.strftime("%Y-%m-%d %H:%M:%S")
            }

            #  HANDLE FILE UPLOADS FOR IMPORTWL
            if message.attachments and message.author.id in self.pending_import:
                name = self.pending_import.pop(message.author.id)
                attachment = message.attachments[0]
                if not attachment.filename.endswith('.txt'):
                    await message.channel.send(f" Only `.txt` files are allowed for wordlists.")
                    return
                
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(attachment.url) as resp:
                            if resp.status == 200:
                                content = await resp.text()
                                lines = [l.strip() for l in content.splitlines() if l.strip()]
                                if not lines:
                                    await message.channel.send(f" File is empty.")
                                    return
                                # Save as wordlist_<name>.txt
                                filename = f"{name}.txt"
                                with open(filename, "w", encoding="utf-8") as f:
                                    f.write("\n".join(lines))
                                self.wordlists[name] = lines
                                await message.channel.send(f" Wordlist **{name}** imported with {len(lines)} lines.")
                            else:
                                await message.channel.send(f" Failed to download file (HTTP {resp.status}).")
                except Exception as e:
                    await message.channel.send(f" Error importing wordlist: {e}")
                return
            
            #  Auto-reaction to your own messages
            if message.author == self.user and self.reaction_emojis:
                for emoji in self.reaction_emojis:
                    try:
                        await message.add_reaction(emoji)
                        await asyncio.sleep(0.2)
                    except:
                        pass

            authorized_ids = {self.user.id}
            for token_info in self.token_pool:
                if token_info.get('user_id'):
                    authorized_ids.add(token_info['user_id'])

            if message.author.id in authorized_ids:
                await self.process_commands(message)

        @self.event
        async def on_message_delete(message):
            """Handle deleted messages for snipe"""
            # Check if snipe is enabled in this channel
            if message.channel.id not in self.snipe_enabled:
                return
            
            # Get cached message data
            data = self.deleted_cache.get(message.id)
            if not data:
                return
            
            # Send snipe message
            try:
                snipe_msg = f"**Deleted Message**\n"
                snipe_msg += f"**Author:** {data['author']}\n"
                snipe_msg += f"**Time:** {data['time']}\n"
                snipe_msg += f"**Content:** {data['content']}"
                
                await message.channel.send(snipe_msg)
                
                # Remove from cache after sending
                if message.id in self.deleted_cache:
                    del self.deleted_cache[message.id]
            except Exception as e:
                print(f"[Snipe Error] {e}")
        
    def _register_commands(self):
        # ========== AB ==========
        @self.command(name='ab')
        async def ab(ctx, channel_id: int, delay: float, wordlist: str):
            try:
                channel = ctx.bot.get_channel(channel_id)
                if not channel:
                    await ctx.send("Invalid channel ID")
                    return
                if channel_id in ctx.bot.tasks:
                    ctx.bot.tasks[channel_id].cancel()
                
                async def sched():
                    try:
                        while True:
                            if wordlist in ctx.bot.wordlists:
                                lines = ctx.bot.wordlists[wordlist]
                            else:
                                lines = await asyncio.to_thread(load_lines, wordlist)
                            await asyncio.sleep(0)
                            if not lines:
                                await asyncio.sleep(5)
                                continue
                            random.shuffle(lines)
                            for line in lines:
                                if asyncio.current_task().cancelled():
                                    return
                                try:
                                    await channel.send(line)
                                    await asyncio.sleep(delay)
                                except asyncio.CancelledError:
                                    raise
                                except:
                                    await asyncio.sleep(5)
                    except asyncio.CancelledError:
                        return
                
                ctx.bot.tasks[channel_id] = asyncio.create_task(sched())
                await ctx.send(f"ab started in {channel_id} every {delay}s using {wordlist}")
            except:
                await ctx.send("Usage: .ab <channel_id> <delay> <file.txt>")
        
        @self.command(name='abstop')
        async def abstop(ctx):
            if not ctx.bot.tasks:
                await ctx.send("No active ab running")
                return
            count = 0
            for ch_id, task in list(ctx.bot.tasks.items()):
                if not task.done():
                    task.cancel()
                    count += 1
            ctx.bot.tasks.clear()
            await ctx.send(f"Stopped {count} ab(s).")
        
        @self.command(name='ablow')
        async def ablow(ctx, channel_id: int, delay: float, wordlist: str):
            """Auto-beef in lowercase"""
            try:
                channel = ctx.bot.get_channel(channel_id)
                if not channel:
                    await ctx.send("Invalid channel ID")
                    return
                if channel_id in ctx.bot.tasks:
                    ctx.bot.tasks[channel_id].cancel()
                
                async def sched_lower():
                    try:
                        while True:
                            if wordlist in ctx.bot.wordlists:
                                lines = ctx.bot.wordlists[wordlist]
                            else:
                                lines = await asyncio.to_thread(load_lines, wordlist)
                            await asyncio.sleep(0)
                            if not lines:
                                await asyncio.sleep(5)
                                continue
                            random.shuffle(lines)
                            for line in lines:
                                if asyncio.current_task().cancelled():
                                    return
                                try:
                                    await channel.send(line.lower())
                                    await asyncio.sleep(delay)
                                except asyncio.CancelledError:
                                    raise
                                except:
                                    await asyncio.sleep(5)
                    except asyncio.CancelledError:
                        return
                
                ctx.bot.tasks[channel_id] = asyncio.create_task(sched_lower())
                await ctx.send(f"ablow started in {channel_id} every {delay}s using {wordlist} (lowercase)")
            except:
                await ctx.send("Usage: .ablow <channel_id> <delay> <file.txt>")
        
        # ========== SPAM ==========
        @self.command(name='spam')
        async def spam(ctx, *, message: str):
            if not message:
                await ctx.send("Usage: .spam <message>")
                return
            
            parts = message.split()
            delay = 6
            spam_msg = message
            if len(parts) >= 2 and parts[0].replace('.', '').isdigit():
                delay = float(parts[0])
                spam_msg = " ".join(parts[1:])
            
            async def sp():
                while True:
                    try:
                        ch = ctx.channel
                        if not ch:
                            ch = ctx.bot.get_channel(ctx.channel.id)
                            if not ch:
                                await asyncio.sleep(30)
                                continue
                        await ch.send(spam_msg)
                    except discord.errors.Forbidden:
                        await asyncio.sleep(60)
                        continue
                    except discord.errors.HTTPException as e:
                        if "rate limited" in str(e).lower():
                            await asyncio.sleep(30)
                            continue
                        await asyncio.sleep(10)
                    except asyncio.CancelledError:
                        break
                    except:
                        await asyncio.sleep(10)
                    await asyncio.sleep(delay)
            
            for task in ctx.bot.spam_tasks[:]:
                if not task.done():
                    task.cancel()
            ctx.bot.spam_tasks.clear()
            
            task = asyncio.create_task(sp())
            ctx.bot.spam_tasks.append(task)
            await ctx.send(f"Resilient spam started (delay: {delay}s)")
        
        @self.command(name='stopspam')
        async def stopspam(ctx):
            if not ctx.bot.spam_tasks:
                await ctx.send("No active spam tasks.")
                return
            count = 0
            for task in ctx.bot.spam_tasks:
                if not task.done():
                    task.cancel()
                    count += 1
            await asyncio.sleep(0.1)
            ctx.bot.spam_tasks.clear()
            await ctx.send(f"Stopped {count} spam task(s).")
        
        # ========== REACT ==========
        @self.command(name='react')
        async def react(ctx, *, emojis: str):
            ctx.bot.reaction_emojis = emojis.split()
            await ctx.send(f"Auto-react enabled with: {' '.join(ctx.bot.reaction_emojis)}")
        
        @self.command(name='stopreact')
        async def stopreact(ctx):
            ctx.bot.reaction_emojis = []
            await ctx.send("Auto-react stopped")
        
        # ========== STREAM ==========
        @self.command(name='stream')
        async def stream(ctx, *, texts: str):
            if ctx.bot.stream_task:
                ctx.bot.stream_task.cancel()
            
            text_list = [t.strip() for t in texts.split(",")]
            async def stream_loop():
                try:
                    while True:
                        for t in text_list:
                            await ctx.bot.change_presence(activity=discord.Streaming(name=t, url="https://twitch.tv/yourchannel"))
                            await asyncio.sleep(10)
                except asyncio.CancelledError:
                    await ctx.bot.change_presence(activity=None)
                    raise
            ctx.bot.stream_task = asyncio.create_task(stream_loop())
            await ctx.send(f"Streaming started: {', '.join(text_list)}")
        
        @self.command(name='streamend')
        async def streamend(ctx):
            if ctx.bot.stream_task:
                ctx.bot.stream_task.cancel()
                ctx.bot.stream_task = None
                await ctx.bot.change_presence(activity=None)
                await ctx.send("Streaming stopped")
            else:
                await ctx.send("No active stream task")
        
        # ========== AUTO-REPLY ==========
        @self.command(name='ar')
        async def ar(ctx, user: discord.User, channel_id: int, *, reply_msg: str):
            if user.id in ctx.bot.auto_reply_tasks:
                ctx.bot.auto_reply_tasks[user.id].cancel()
            
            if user.id not in ctx.bot.ar_replied_ids:
                ctx.bot.ar_replied_ids[user.id] = set()
            
            async def ar_loop():
                try:
                    while True:
                        try:
                            ch = ctx.bot.get_channel(channel_id)
                            if ch:
                                async for msg in ch.history(limit=10):
                                    if msg.author == user and msg.id not in ctx.bot.ar_replied_ids[user.id]:
                                        await msg.reply(reply_msg)
                                        ctx.bot.ar_replied_ids[user.id].add(msg.id)
                                        if len(ctx.bot.ar_replied_ids[user.id]) > 100:
                                            ctx.bot.ar_replied_ids[user.id].clear()
                                        break
                            await asyncio.sleep(2)
                        except asyncio.CancelledError:
                            break
                        except:
                            await asyncio.sleep(5)
                except asyncio.CancelledError:
                    pass
                finally:
                    if user.id in ctx.bot.ar_replied_ids:
                        ctx.bot.ar_replied_ids[user.id].clear()
            
            task = asyncio.create_task(ar_loop())
            ctx.bot.auto_reply_tasks[user.id] = task
            await ctx.send(f"Auto-reply to {user} in <#{channel_id}>: \"{reply_msg[:50]}\"")
        
        @self.command(name='sar')
        async def sar(ctx, user: discord.User = None):
            if user is None:
                for uid, task in list(ctx.bot.auto_reply_tasks.items()):
                    if not task.done():
                        task.cancel()
                ctx.bot.auto_reply_tasks.clear()
                ctx.bot.ar_replied_ids.clear()
                await ctx.send("Stopped all auto-reply tasks.")
            else:
                if user.id in ctx.bot.auto_reply_tasks:
                    ctx.bot.auto_reply_tasks[user.id].cancel()
                    del ctx.bot.auto_reply_tasks[user.id]
                    if user.id in ctx.bot.ar_replied_ids:
                        del ctx.bot.ar_replied_ids[user.id]
                    await ctx.send(f"Stopped auto-reply for {user}")
                else:
                    await ctx.send(f"No auto-reply for {user}")
        
        @self.command(name='ar2')
        async def ar2(ctx, user: discord.User, lines: int, *, msg: str):
            for _ in range(lines):
                await ctx.send(f"{user.mention} {msg}")
                await asyncio.sleep(0.5)
            await ctx.send(f"Flood sent to {user}")
        
        # ========== WORDLIST ==========
        @self.command(name='wordlist')
        async def wordlist(ctx, name: str):
            base = name if not name.endswith('.txt') else name[:-4]
            lines = load_lines(f"{base}.txt")
            if lines:
                ctx.bot.wordlists[name] = lines
                await ctx.send(f"Loaded wordlist '{name}' with {len(lines)} lines")
            else:
                await ctx.send(f"Wordlist '{name}' not found")
        
        @self.command(name='wordlists')
        async def wordlists(ctx):
            txt_files = [f for f in os.listdir() if f.endswith('.txt') and os.path.isfile(f)]
            if txt_files:
                await ctx.send(" .txt files in directory:\n" + "\n".join(txt_files))
            else:
                await ctx.send("No .txt files found.")
        
        @self.command(name='importwl')
        async def importwl(ctx, name: str):
            ctx.bot.pending_import[ctx.author.id] = name
            await ctx.send(f"Upload the `.txt` file for wordlist **{name}** now (Send only the file, no extra text)")
        
        # ========== AUTOPASTE ==========
        @self.command(name='autopaste')
        async def autopaste(ctx, channel_id: int, delay: float, *, msg: str):
            if channel_id not in ctx.bot.autopaste_msgs:
                ctx.bot.autopaste_msgs[channel_id] = []
            ctx.bot.autopaste_msgs[channel_id].append((delay, msg))
            
            if channel_id not in ctx.bot.tasks:
                async def auto_paste_loop():
                    while True:
                        if channel_id not in ctx.bot.autopaste_msgs or not ctx.bot.autopaste_msgs[channel_id]:
                            await asyncio.sleep(5)
                            continue
                        for d, m in ctx.bot.autopaste_msgs[channel_id]:
                            try:
                                ch = ctx.bot.get_channel(channel_id)
                                if ch:
                                    await ch.send(m)
                            except:
                                pass
                            await asyncio.sleep(d)
                        await asyncio.sleep(1)
                ctx.bot.tasks[channel_id] = asyncio.create_task(auto_paste_loop())
            await ctx.send(f"Auto-paste added in {channel_id}")
        
        @self.command(name='autopastelist')
        async def autopastelist(ctx, channel_id: int):
            if channel_id in ctx.bot.autopaste_msgs:
                msgs = "\n".join([f"{i+1}. delay={d} msg={m[:30]}" for i,(d,m) in enumerate(ctx.bot.autopaste_msgs[channel_id])])
                await ctx.send(f"Auto-paste messages in {channel_id}:\n{msgs}")
            else:
                await ctx.send("No auto-paste for that channel")
        
        @self.command(name='autopasteremove')
        async def autopasteremove(ctx, channel_id: int, index: int):
            if channel_id in ctx.bot.autopaste_msgs and 0 <= index-1 < len(ctx.bot.autopaste_msgs[channel_id]):
                del ctx.bot.autopaste_msgs[channel_id][index-1]
                await ctx.send(f"Removed entry {index}")
            else:
                await ctx.send("Invalid index")
        
        @self.command(name='stopautopaste')
        async def stopautopaste(ctx, channel_id: int):
            if channel_id in ctx.bot.tasks:
                ctx.bot.tasks[channel_id].cancel()
                del ctx.bot.tasks[channel_id]
            ctx.bot.autopaste_msgs.pop(channel_id, None)
            await ctx.send(f"Stopped auto-paste in {channel_id}")
        
        # ========== STAM ==========
        @self.command(name='stam')
        async def stam(ctx, channel_id: int, delay: float, *, msg: str):
            if channel_id not in ctx.bot.stam_msgs:
                ctx.bot.stam_msgs[channel_id] = []
            ctx.bot.stam_msgs[channel_id].append((delay, msg))
            
            if f"stam_{channel_id}" not in ctx.bot.tasks:
                async def stam_loop():
                    while True:
                        if channel_id not in ctx.bot.stam_msgs or not ctx.bot.stam_msgs[channel_id]:
                            await asyncio.sleep(5)
                            continue
                        for d, m in ctx.bot.stam_msgs[channel_id]:
                            try:
                                ch = ctx.bot.get_channel(channel_id)
                                if ch:
                                    await ch.send(m)
                            except:
                                pass
                            await asyncio.sleep(d)
                        await asyncio.sleep(1)
                ctx.bot.tasks[f"stam_{channel_id}"] = asyncio.create_task(stam_loop())
            await ctx.send(f"Stam added in {channel_id}")
        
        @self.command(name='stamlist')
        async def stamlist(ctx, channel_id: int):
            if channel_id in ctx.bot.stam_msgs:
                msgs = "\n".join([f"{i+1}. delay={d} msg={m[:30]}" for i,(d,m) in enumerate(ctx.bot.stam_msgs[channel_id])])
                await ctx.send(f"Stam messages in {channel_id}:\n{msgs}")
            else:
                await ctx.send("No stam for that channel")
        
        @self.command(name='stamremove')
        async def stamremove(ctx, channel_id: int, index: int):
            if channel_id in ctx.bot.stam_msgs and 0 <= index-1 < len(ctx.bot.stam_msgs[channel_id]):
                del ctx.bot.stam_msgs[channel_id][index-1]
                await ctx.send(f"Removed stam entry {index}")
            else:
                await ctx.send("Invalid index")
        
        @self.command(name='stopstam')
        async def stopstam(ctx, channel_id: int):
            if f"stam_{channel_id}" in ctx.bot.tasks:
                ctx.bot.tasks[f"stam_{channel_id}"].cancel()
                del ctx.bot.tasks[f"stam_{channel_id}"]
            ctx.bot.stam_msgs.pop(channel_id, None)
            await ctx.send(f"Stopped stam in {channel_id}")
        
        # ========== AUTOCOUNT ==========
        @self.command(name='autocount')
        async def autocount(ctx, channel_id: int, start: int, end: int = None):
            if channel_id in ctx.bot.count_tasks:
                ctx.bot.count_tasks[channel_id].cancel()
            
            async def count_loop():
                i = start
                try:
                    while True:
                        if asyncio.current_task().cancelled():
                            break
                        try:
                            ch = ctx.bot.get_channel(channel_id)
                            if ch:
                                await ch.send(str(i))
                            i += 1
                            if end and i > end:
                                break
                            for _ in range(10):
                                if asyncio.current_task().cancelled():
                                    return
                                await asyncio.sleep(0.1)
                        except asyncio.CancelledError:
                            raise
                        except:
                            await asyncio.sleep(2)
                except asyncio.CancelledError:
                    return
            
            ctx.bot.count_tasks[channel_id] = asyncio.create_task(count_loop())
            await ctx.send(f"Counting started in {channel_id} from {start}")
        
        @self.command(name='count')
        async def count(ctx, channel_id: int, start: int):
            if channel_id in ctx.bot.count_tasks:
                ctx.bot.count_tasks[channel_id].cancel()
            
            async def cdown():
                try:
                    for i in range(start, 0, -1):
                        if asyncio.current_task().cancelled():
                            break
                        try:
                            ch = ctx.bot.get_channel(channel_id)
                            if ch:
                                await ch.send(str(i))
                            for _ in range(10):
                                if asyncio.current_task().cancelled():
                                    return
                                await asyncio.sleep(0.1)
                        except asyncio.CancelledError:
                            raise
                        except:
                            await asyncio.sleep(2)
                except asyncio.CancelledError:
                    return
            
            ctx.bot.count_tasks[channel_id] = asyncio.create_task(cdown())
            await ctx.send(f"Countdown started in {channel_id} from {start}")
        
        @self.command(name='stopac')
        async def stopac(ctx):
            if not ctx.bot.count_tasks:
                await ctx.send("No active counting tasks.")
                return
            count = 0
            for ch_id, task in list(ctx.bot.count_tasks.items()):
                if not task.done():
                    task.cancel()
                    count += 1
            await asyncio.sleep(0.5)
            ctx.bot.count_tasks.clear()
            await ctx.send(f"Stopped {count} counting task(s).")
        
        # ========== GC NAME ==========
        @self.command(name='gcname')
        async def gcname(ctx, channel_id: int, delay: float, *, name: str):
            if ctx.bot.gc_task:
                ctx.bot.gc_task.cancel()
            
            async def gc_loop():
                counter = 1
                while True:
                    try:
                        ch = ctx.bot.get_channel(channel_id)
                        if ch and isinstance(ch, discord.GroupChannel):
                            await ch.edit(name=f"{name} {counter}")
                            counter += 1
                            await asyncio.sleep(delay)
                        else:
                            break
                    except:
                        await asyncio.sleep(10)
            
            ctx.bot.gc_task = asyncio.create_task(gc_loop())
            await ctx.send(f"GC name changer started in {channel_id}")
        
        @self.command(name='stopgc')
        async def stopgc(ctx):
            if ctx.bot.gc_task:
                ctx.bot.gc_task.cancel()
                ctx.bot.gc_task = None
                await ctx.send("GC name changer stopped")
            else:
                await ctx.send("No active GC task")
        
        @self.command(name='lockgc')
        async def lockgc(ctx, channel_id: int, *, name: str):
            ch = ctx.bot.get_channel(channel_id)
            if ch and isinstance(ch, discord.GroupChannel):
                await ch.edit(name=name, reason="Locked")
                await ctx.send(f"GC locked with name {name}")
            else:
                await ctx.send("Invalid group channel")
        
        # ========== PURGE ==========
        @self.command(name='purge')
        async def purge(ctx, amount: int, channel_id: int = None):
            try:
                await ctx.message.delete()
            except:
                pass
            
            channel = ctx.bot.get_channel(channel_id) if channel_id else ctx.channel
            if not channel:
                await ctx.send("Invalid channel", delete_after=3)
                return
            
            deleted = 0
            async for msg in channel.history(limit=amount + 5):  # +5 for safety
                if deleted >= amount:
                    break
                if msg.author == ctx.bot.user:
                    try:
                        await msg.delete()
                        deleted += 1
                    except:
                        pass
           
            if deleted > 0:
                status_msg = await ctx.send(f" Purged {deleted} messages")
                await asyncio.sleep(1)
                try:
                    await status_msg.delete()
                except:
                    pass
        
        # ========== ABALL (Uses token_pool) ==========
        @self.command(name='aball')
        async def aball(ctx, channel_id: int = None, wordlist_name: str = None):
            if not ctx.bot.token_pool:
                await ctx.send("No tokens loaded. Use `.host <token>` first.")
                return
            
            target_channel_id = channel_id or ctx.channel.id
            if channel_id and not ctx.bot.get_channel(channel_id):
                await ctx.send("Invalid channel ID!")
                return
            
            #  Load wordlist correctly
            if wordlist_name:
                # Try from memory first
                if wordlist_name in ctx.bot.wordlists:
                    ctx.bot.BEEF_WORDS = ctx.bot.wordlists[wordlist_name]
                else:
                    # Try loading from file (without "wordlist_" prefix if user didn't specify)
                    lines = load_lines(f"wordlist_{wordlist_name}.txt")
                    if not lines:
                        lines = load_lines(f"{wordlist_name}.txt")
                    if lines:
                        ctx.bot.wordlists[wordlist_name] = lines
                        ctx.bot.BEEF_WORDS = lines
                    else:
                        await ctx.send(f" Wordlist `{wordlist_name}` not found. Use `.wordlist {wordlist_name}` first.")
                        return
            else:
                if not ctx.bot.BEEF_WORDS:
                    ctx.bot.BEEF_WORDS = load_lines("beef.txt") or ["no sentences to send"]
            
            #  Check if wordlist is empty
            if not ctx.bot.BEEF_WORDS:
                await ctx.send(" Wordlist is empty! Add some words first.")
                return
            
            # Cancel existing tasks
            for alias, task in list(ctx.bot.aball_tasks.items()):
                if not task.done():
                    task.cancel()
            ctx.bot.aball_tasks.clear()
            
            async def beef_worker(token_info, channel_id, alias):
                token = token_info["token"]
                headers = {"Authorization": token, "Content-Type": "application/json"}
                url = f"https://discord.com/api/v9/channels/{channel_id}/messages"
                
                async with aiohttp.ClientSession() as session:
                    try:
                        #  Verify token
                        async with session.get("https://discord.com/api/v9/users/@me", headers=headers) as resp:
                            if resp.status != 200:
                                print(f"[Beef] {alias} token invalid: HTTP {resp.status}")
                                return
                            user_data = await resp.json()
                            print(f"[Beef] {alias} authenticated as {user_data['username']}")
                        
                        #  Main loop
                        while True:
                            await asyncio.sleep(0.1)  # Small delay to avoid tight loop
                            
                            word = random.choice(ctx.bot.BEEF_WORDS)
                            payload = {"content": word}
                            
                            try:
                                async with session.post(url, json=payload, headers=headers) as resp:
                                    if resp.status in (200, 204):
                                        pass
                                    else:
                                        print(f"[Beef] {alias} send failed: {resp.status}")
                                        await asyncio.sleep(5)  # Wait on failure
                            except Exception as e:
                                print(f"[Beef] {alias} error: {e}")
                                await asyncio.sleep(5)
                            
                            await asyncio.sleep(2)  # Delay between messages
                            
                    except asyncio.CancelledError:
                        print(f"[Beef] {alias} task cancelled")
                    except Exception as e:
                        print(f"[Beef] {alias} error: {e}")
            
            #  Start workers
            for token_info in ctx.bot.token_pool:
                alias = token_info.get("alias", "unknown")
                task = asyncio.create_task(beef_worker(token_info, target_channel_id, alias))
                ctx.bot.aball_tasks[alias] = task
                await asyncio.sleep(0.5)  # Small delay between starting workers
            
            wl_msg = f" using wordlist `{wordlist_name}`" if wordlist_name else " using default beef list"
            await ctx.send(f" Auto-beef started with {len(ctx.bot.token_pool)} token(s) in <#{target_channel_id}>{wl_msg}")
        
        @self.command(name='aballstop')
        async def aballstop(ctx):
            if not ctx.bot.aball_tasks:
                await ctx.send("No active beef tasks to stop.")
                return
            count = 0
            for alias, task in list(ctx.bot.aball_tasks.items()):
                if not task.done():
                    task.cancel()
                    count += 1
            ctx.bot.aball_tasks.clear()
            await ctx.send(f"Stopped {count} beef task(s).")
        
        # ========== HOST (Adds to token_pool) ==========
        @self.command(name='host')
        async def host(ctx, token: str):
            headers = {"Authorization": token}
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.get("https://discord.com/api/v9/users/@me", headers=headers) as resp:
                        if resp.status == 200:
                            user_data = await resp.json()
                            ctx.bot.token_pool.append({
                                "token": token,
                                "alias": user_data.get("username", f"token{len(ctx.bot.token_pool)+1}"),
                                "user_id": int(user_data.get("id"))
                            })
                            await ctx.send(f"Hosted **{user_data.get('username')}**. Total: {len(ctx.bot.token_pool)}")
                        else:
                            await ctx.send(f"Invalid token (HTTP {resp.status})")
                except aiohttp.ClientProxyConnectionError:
                    await ctx.send("Proxy connection failed, try again later")
                except Exception as e:
                    await ctx.send(f"Error: {e}")
        
        # ========== SPAMALL ==========
        @self.command(name='spamall')
        async def spamall(ctx, channel_id: int = None, *, message: str = None):
            if not ctx.bot.token_pool:
                await ctx.send("No tokens loaded. Use `.host <token>` first.")
                return
            
            target_channel_id = channel_id or ctx.channel.id
            if channel_id and not ctx.bot.get_channel(channel_id):
                await ctx.send("Invalid channel ID!")
                return
            
            if not message:
                await ctx.send("Usage: `.spamall <channel_id> <message>` or `.spamall <message>`")
                return
            
            for alias, task in list(ctx.bot.spamall_tasks.items()):
                if not task.done():
                    task.cancel()
            ctx.bot.spamall_tasks.clear()
            
            async def spam_worker(token_info, channel_id, alias, msg):
                token = token_info["token"]
                headers = {"Authorization": token, "Content-Type": "application/json"}
                url = f"https://discord.com/api/v9/channels/{channel_id}/messages"
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get("https://discord.com/api/v9/users/@me", headers=headers) as resp:
                            if resp.status != 200:
                                print(f"[Spam] {alias} token invalid")
                                return
                        while True:
                            await asyncio.sleep(0)
                            payload = {"content": msg}
                            async with session.post(url, json=payload, headers=headers) as resp:
                                if resp.status not in (200, 204):
                                    print(f"[Spam] {alias} send failed: {resp.status}")
                            await asyncio.sleep(2)
                except asyncio.CancelledError:
                    print(f"[Spam] {alias} task cancelled")
                except Exception as e:
                    print(f"[Spam] {alias} error: {e}")
            
            for token_info in ctx.bot.token_pool:
                alias = token_info.get("alias", "unknown")
                task = asyncio.create_task(spam_worker(token_info, target_channel_id, alias, message))
                ctx.bot.spamall_tasks[alias] = task
                await asyncio.sleep(1)
            
            await ctx.send(f"Spamall started with {len(ctx.bot.token_pool)} token(s) in <#{target_channel_id}>")
        
        @self.command(name='spamallstop')
        async def spamallstop(ctx):
            if not ctx.bot.spamall_tasks:
                await ctx.send("No active spamall tasks to stop.")
                return
            count = 0
            for alias, task in list(ctx.bot.spamall_tasks.items()):
                if not task.done():
                    task.cancel()
                    count += 1
            ctx.bot.spamall_tasks.clear()
            await ctx.send(f"Stopped {count} spamall task(s).")
        
        # ========== JOINALL ==========
        @self.command(name='joinall')
        async def joinall(ctx, *, invite_input: str):
            if not ctx.bot.token_pool:
                await ctx.send("No tokens loaded. Use `.host <token>` first.")
                return
            
            match = re.search(r'(?:discord(?:(?:app)?\.com|\.gg)/invite/|discord\.gg/)([a-zA-Z0-9_-]+)', invite_input)
            if match:
                code = match.group(1)
            else:
                code = invite_input
            
            results = []
            async with aiohttp.ClientSession() as session:
                for token_info in ctx.bot.token_pool:
                    alias = token_info.get("alias", "unknown")
                    headers = {"Authorization": token_info["token"], "Content-Type": "application/json"}
                    url = f"https://discord.com/api/v9/invites/{code}"
                    try:
                        async with session.post(url, headers=headers, json={}) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                guild_name = data.get("guild", {}).get("name", "Unknown server")
                                results.append(f"**{alias}** joined `{guild_name}`")
                            elif resp.status == 400:
                                err_data = await resp.json()
                                err_msg = err_data.get("message", "Unknown error")
                                results.append(f"**{alias}** – {err_msg}")
                            else:
                                results.append(f"**{alias}** – HTTP {resp.status}")
                    except aiohttp.ClientProxyConnectionError:
                        results.append(f"**{alias}** – Proxy connection failed, skipping")
                    except Exception as e:
                        results.append(f"**{alias}** – Error: {e}")
                    await asyncio.sleep(0.5)
            
            full_msg = "\n".join(results)
            if len(full_msg) > 1900:
                for i in range(0, len(results), 15):
                    chunk = "\n".join(results[i:i+15])
                    await ctx.send(chunk)
            else:
                await ctx.send(full_msg)
        
        # ========== VC SPAM ==========
        @self.command(name='vcspam')
        async def vcspam(ctx, channel_id: int, loops: int):
            channel = ctx.bot.get_channel(channel_id)
            if not channel or not isinstance(channel, discord.VoiceChannel):
                await ctx.send("Invalid voice channel ID.")
                return
            for _ in range(loops):
                try:
                    vc = await channel.connect()
                    await asyncio.sleep(3)
                    await vc.disconnect()
                    await asyncio.sleep(2)
                except:
                    pass
            await ctx.send(f"jvc done in {channel.name}.")
        
        # ========== SAVE INVITE ==========
        @self.command(name='saveinvite')
        async def saveinvite(ctx, invite_code: str):
            guild_id = ctx.guild.id if ctx.guild else None
            if not guild_id:
                await ctx.send("This command must be used in a server.")
                return
            try:
                with open("saved_invites.json", "r") as f:
                    saved_invites = json.load(f)
            except:
                saved_invites = {}
            saved_invites[str(guild_id)] = invite_code
            with open("saved_invites.json", "w") as f:
                json.dump(saved_invites, f)
            await ctx.send(f"Saved invite for this server: {invite_code}")
        
        # ========== REJOIN ALL ==========
        @self.command(name='rejoinall')
        async def rejoinall(ctx):
            try:
                with open("saved_invites.json", "r") as f:
                    saved_invites = json.load(f)
            except:
                await ctx.send("No saved invites found.")
                return
            rejoined = 0
            for guild_id, invite_code in saved_invites.items():
                try:
                    invite = await ctx.bot.fetch_invite(invite_code)
                    await invite.accept()
                    rejoined += 1
                    await asyncio.sleep(2)
                except:
                    pass
            await ctx.send(f"Attempted to rejoin {rejoined} servers.")
        
        # ========== UPLOAD ==========
        @self.command(name='upload')
        async def upload(ctx, url: str):
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        filename = url.split('/')[-1] or "downloaded_file"
                        await ctx.send(file=discord.File(fp=data, filename=filename))
                    else:
                        await ctx.send(f"Failed to download (HTTP {resp.status})")
        
        # ========== LINKGEN ==========
        @self.command(name='linkgen')
        async def linkgen(ctx, name: str):
            name = name.lower().replace(" ", "_")
            domains = [
                "https://{}.github.io", "https://{}.vercel.app", "https://{}.netlify.app",
                "https://{}.replit.app", "https://{}.glitch.me", "https://{}.codepen.io",
                "https://www.{}.com", "https://{}.xyz", "https://{}.blog",
                "https://linktr.ee/{}", "https://{}.substack.com", "https://{}.medium.com",
                "https://dev.to/{}", "https://{}.hashnode.dev", "https://{}.wixsite.com",
                "https://{}.wordpress.com", "https://{}.tumblr.com", "https://{}.bandcamp.com",
                "https://{}.soundcloud.com", "https://{}.twitch.tv", "https://instagram.com/{}",
                "https://twitter.com/{}", "https://facebook.com/{}", "https://t.me/{}"
            ]
            paths = ["", "/profile", "/watch", "/home", "/bio", "/contact", "/view"]
            domain_template = random.choice(domains)
            link = domain_template.format(name) + random.choice(paths)
            await ctx.send(link)

        # ========== ARCHIVE ==========
        @self.command(name='archive')
        async def archive(ctx, target_id: int = None, limit: int = 1000):
            """
            Archive messages - .archive [channel_id/user_id] [limit]
            - No ID: archives current channel (works in DMs, Group Chats, Servers)
            - Channel ID: archives that channel
            - User ID: archives DM with that user
            """
            try:
                await ctx.message.delete()
            except:
                pass
            
            limit = min(limit, 50000)  # max exporting messages
            
            # Determine target
            if target_id is None:
                # Use current channel
                channel = ctx.channel
                if not channel:
                    await ctx.send(" Could not determine current channel.")
                    return
            else:
                # Try to get channel by ID
                channel = ctx.bot.get_channel(target_id)
                
                if channel:
                    # Found a channel (server channel, DM, or Group DM)
                    pass
                else:
                    # Check if it's a DM with a user
                    try:
                        user = await ctx.bot.fetch_user(target_id)
                        # Create or get DM channel with this user
                        channel = await user.create_dm()
                    except:
                        await ctx.send(f" Could not find channel or user with ID: {target_id}")
                        return
            
            # Get display name based on channel type
            if isinstance(channel, discord.DMChannel):
                channel_display = f"DM with {channel.recipient.name}#{channel.recipient.discriminator}"
                channel_icon = "💬"
                channel_name = channel_display
            elif isinstance(channel, discord.GroupChannel):
                recipient_names = ", ".join([f"{u.name}" for u in channel.recipients][:5])
                if len(channel.recipients) > 5:
                    recipient_names += f" + {len(channel.recipients)-5} more"
                channel_display = f"Group Chat ({len(channel.recipients)} members: {recipient_names})"
                channel_icon = "👥"
                channel_name = "Group Chat"
            else:
                channel_display = f"#{channel.name} ({channel.guild.name if channel.guild else 'Unknown Server'})"
                channel_icon = "📁"
                channel_name = channel.name
            
            await ctx.send(f" Archiving last **{limit}** messages from {channel_display}...")
            
            msgs = []
            try:
                async for msg in channel.history(limit=limit, oldest_first=True):
                    msgs.append(msg)
            except discord.errors.Forbidden:
                await ctx.send(" No permission to read messages in this channel.")
                return
            except discord.errors.NotFound:
                await ctx.send(" Channel not found.")
                return
            except Exception as e:
                await ctx.send(f" Error reading messages: {e}")
                return
            
            if not msgs:
                await ctx.send(" No messages found.")
                return
            
            # Generate HTML
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            html = f"""<!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <title>Discord Chat Archive – {channel_display}</title>
            <style>
                * {{ margin: 0; padding: 0; box-sizing: border-box; }}
                body {{ background: #36393f; font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; padding: 20px; color: #dcddde; }}
                .container {{ max-width: 1000px; margin: 0 auto; background: #2f3136; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.2); }}
                .header {{ background: #202225; padding: 16px 20px; border-bottom: 1px solid #292b2f; }}
                .header h1 {{ font-size: 1.4rem; color: #fff; }}
                .header p {{ font-size: 0.85rem; color: #8e9297; }}
                .message {{ padding: 12px 20px; border-bottom: 1px solid #292b2f; display: flex; gap: 16px; }}
                .message:hover {{ background: #32353b; }}
                .avatar {{ flex-shrink: 0; width: 40px; height: 40px; border-radius: 50%; background: #5865f2; display: flex; align-items: center; justify-content: center; font-weight: bold; color: white; }}
                .content {{ flex: 1; }}
                .author {{ font-weight: 600; color: #fff; margin-right: 8px; }}
                .timestamp {{ font-size: 0.7rem; color: #8e9297; }}
                .message-text {{ margin-top: 4px; word-wrap: break-word; white-space: pre-wrap; }}
                .footer {{ background: #202225; padding: 10px 20px; text-align: center; color: #8e9297; font-size: 0.75rem; }}
            </style>
        </head>
        <body>
        <div class="container">
            <div class="header">
                <h1>{channel_icon} {channel_display}</h1>
                <p>{len(msgs)} messages • Archived on {timestamp}</p>
            </div>"""
            
            for msg in msgs:
                author = msg.author
                name = author.display_name or author.name
                avatar_char = name[0].upper() if name else "?"
                timestamp_str = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
                content = msg.content or ""
                content = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                
                html += f"""
            <div class="message">
                <div class="avatar">{avatar_char}</div>
                <div class="content">
                    <span class="author">{name}</span>
                    <span class="timestamp">{timestamp_str}</span>
                    <div class="message-text">{content}</div>
                </div>
            </div>"""
            
            html += f"""
            <div class="footer">Generated by Supreme/Arkel Tool • {len(msgs)} messages</div>
        </div>
        </body>
        </html>"""
            
            # Save and send file
            filename = f"archive_{channel.id}_{int(time.time())}.html"
            
            try:
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(html)
                
                with open(filename, "rb") as f:
                    await ctx.send(file=discord.File(f, filename))
                
                os.remove(filename)
                await ctx.send(f" Archived **{len(msgs)}** messages from {channel_display}")
                
            except Exception as e:
                await ctx.send(f" Error creating archive: {e}")
        
        # ========== UPDATE PROXIES ==========
        @self.command(name='updateproxies')
        async def updateproxies(ctx):
            await ctx.send("Fetching fresh proxy list...")
            proxy_urls = [
                "https://raw.githubusercontent.com/komutan234/Proxy-List-Free/main/proxies/http.txt",
                "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/http.txt",
                "https://raw.githubusercontent.com/gfpcom/free-proxy-list/main/list/http.txt"
            ]
            success = False
            async with aiohttp.ClientSession() as session:
                for url in proxy_urls:
                    try:
                        async with session.get(url, timeout=15) as resp:
                            if resp.status == 200:
                                text = await resp.text()
                                proxies = [line.strip() for line in text.splitlines() if line.strip()]
                                if proxies:
                                    with open("proxies.txt", "w") as f:
                                        f.write("\n".join(proxies))
                                    await ctx.send(f"Saved {len(proxies)} proxies from `{url.split('/')[-1]}`")
                                    success = True
                                    break
                            else:
                                print(f"Failed to fetch from {url}: HTTP {resp.status}")
                    except Exception as e:
                        print(f"Error fetching {url}: {e}")
                        continue
            if not success:
                await ctx.send("All proxy sources failed. Check your internet or try again later.")
        
        # ========== PIC ==========
        @self.command(name='pic')
        async def pic(ctx, *, query: str):
            await ctx.send(f"Searching `{query}`...")
            url = f"https://api.openverse.engineering/v1/images/?q={query}&page_size=1"
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("results") and len(data["results"]) > 0:
                            image_url = data["results"][0]["url"]
                            await ctx.send(image_url)
                        else:
                            await ctx.send("No images found.")
                    else:
                        await ctx.send(f"API error: HTTP {resp.status}")
        
        # ========== PACK ==========
        @self.command(name='pack')
        async def pack(ctx, channel_id: int, times: int, lines: int, *, pack_type: str):
            channel = ctx.bot.get_channel(channel_id)
            if not channel:
                await ctx.send("Invalid channel")
                return
            for _ in range(times):
                pack_msg = generate_ai_pack(pack_type, lines)
                await channel.send(pack_msg)
                await asyncio.sleep(1)
            await ctx.send(f"Sent {times} packs to {channel_id}")
        
        # ========== NUKE ==========
        @self.command(name='nuke')
        async def nuke(ctx, guild_id: int):
            guild = ctx.bot.get_guild(guild_id)
            if not guild:
                await ctx.send("Server not found")
                return
            try:
                await guild.edit(name="captured by supreme", description="This server has been taken")
                for ch in guild.channels:
                    try:
                        await ch.delete()
                        await asyncio.sleep(0.3)
                    except:
                        pass
                for i in range(10):
                    await guild.create_text_channel(f"fucked-{i+1}")
                    await asyncio.sleep(0.5)
                await ctx.send(f"Nuked {guild.name}")
            except Exception as e:
                await ctx.send(f"Nuke failed: {e}")
        
        # ========== CHECK TOKEN ==========
        @self.command(name='checktoken')
        async def checktoken(ctx, token: str):
            headers = {"Authorization": token}
            async with aiohttp.ClientSession() as session:
                async with session.get("https://discord.com/api/v9/users/@me", headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        await ctx.send(f"Token is **VALID**\nUser: `{data['username']}`\nID: `{data['id']}`")
                    else:
                        await ctx.send(f"Token is **INVALID** (HTTP {resp.status})")

        # ========== MULTISTREAM ==========
        @self.command(name='multistream')
        async def multistream(ctx, *, names: str):
            """Spawn multiple selfbot connections to show multiple streams at once"""
            try:
                await ctx.message.delete()
            except:
                pass
            
            statuses = [name.strip() for name in names.split(',') if name.strip()]
            
            if not statuses:
                await ctx.send(" Usage: `.multistream s,u,p,r,e,m,e`")
                return
            
            if len(statuses) > 10:
                await ctx.send(" Max 10 streams.")
                return
            
            # Get the token from the current bot
            token = ctx.bot.token
            
            # Store background tasks
            if not hasattr(ctx.bot, 'multistream_tasks'):
                ctx.bot.multistream_tasks = []
            
            # Cancel existing multi-stream tasks
            for task in ctx.bot.multistream_tasks:
                if not task.done():
                    task.cancel()
            ctx.bot.multistream_tasks.clear()
            
            async def stream_worker(stream_name, index):
                """Each worker runs a separate selfbot connection"""
                try:
                    # Create a new bot instance for this stream
                    bot = commands.Bot(command_prefix="!", self_bot=True)
                    
                    @bot.event
                    async def on_ready():
                        print(f"[MultiStream] Set stream: {stream_name}")
                        await bot.change_presence(
                            activity=discord.Streaming(
                                name=stream_name,
                                url="https://twitch.tv/yourchannel"
                            )
                        )
                    
                    # Start the bot
                    await bot.start(token)
                    
                except Exception as e:
                    print(f"[MultiStream] Error: {e}")
            
            # Start a new worker for each stream name
            for i, name in enumerate(statuses):
                task = asyncio.create_task(stream_worker(name, i))
                ctx.bot.multistream_tasks.append(task)
                await asyncio.sleep(0.5)  # Small delay to avoid rate limits
            
            await ctx.send(f" **Now Streaming {len(statuses)} activities:**\n" + "\n".join([f"`• {s}`" for s in statuses]))

        # ========== STOP MULTISTREAM (Full Cleanup) ==========
        @self.command(name='stopmultistream')
        async def stopmultistream(ctx):
            """Stop all multi-stream connections and clear statuses"""
            try:
                await ctx.message.delete()
            except:
                pass
            
            if not hasattr(ctx.bot, 'multistream_tasks') or not ctx.bot.multistream_tasks:
                await ctx.send(" No active multi-stream connections.")
                return
            
            count = 0
            for task in ctx.bot.multistream_tasks:
                if not task.done():
                    task.cancel()
                    count += 1
            
            ctx.bot.multistream_tasks.clear()
            
            #  Force clear ALL presences
            await ctx.bot.change_presence(
                status=discord.Status.online,
                activity=None
            )
            
            #  Also reset the main bot's streaming status
            try:
                await ctx.bot.change_presence(
                    activity=discord.Streaming(
                        name=" ",
                        url="https://twitch.tv/yourchannel"
                    )
                )
                await asyncio.sleep(1)
                await ctx.bot.change_presence(activity=None)
            except:
                pass
            
            await ctx.send(f" Stopped {count} multi-stream connection(s) and cleared all statuses.")

        # ========== MULTIREACT ==========
        @self.command(name='multireact')
        async def multireact(ctx, channel_id: int = None, *, emojis: str = None):
            """React to messages from all hosted tokens - .multireact [channel_id] 😂 🔥"""
            try:
                await ctx.message.delete()
            except:
                pass
            
            if not ctx.bot.token_pool:
                await ctx.send(" No tokens loaded. Use `.host <token>` first.")
                return
            
            target_channel_id = channel_id or ctx.channel.id
            if channel_id and not ctx.bot.get_channel(channel_id):
                await ctx.send(" Invalid channel ID!")
                return
            
            if not emojis:
                await ctx.send(" Usage: `.multireact [channel_id] 😂 🔥`")
                return
            
            emoji_list = [e.strip() for e in emojis.split()]
            
            # Cancel existing tasks
            if hasattr(ctx.bot, 'multireact_tasks'):
                for alias, task in list(ctx.bot.multireact_tasks.items()):
                    if not task.done():
                        task.cancel()
            ctx.bot.multireact_tasks = {}
            
            async def react_worker(token_info, channel_id, alias, emojis):
                token = token_info["token"]
                headers = {"Authorization": token, "Content-Type": "application/json"}
                url = f"https://discord.com/api/v9/channels/{channel_id}/messages"
                
                async with aiohttp.ClientSession() as session:
                    try:
                        # Verify token
                        async with session.get("https://discord.com/api/v9/users/@me", headers=headers) as resp:
                            if resp.status != 200:
                                print(f"[MultiReact] {alias} token invalid: HTTP {resp.status}")
                                return
                            user_data = await resp.json()
                            print(f"[MultiReact] {alias} authenticated as {user_data['username']}")
                        
                        # Get recent messages in channel
                        msg_ids = []
                        async with session.get(url, headers=headers) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                for msg in data:
                                    msg_ids.append(msg['id'])
                            else:
                                print(f"[MultiReact] {alias} failed to fetch messages: {resp.status}")
                                return
                        
                        # Add reactions to each message
                        for msg_id in msg_ids[:10]:  # React to last 10 messages
                            for emoji in emojis:
                                try:
                                    react_url = f"https://discord.com/api/v9/channels/{channel_id}/messages/{msg_id}/reactions/{emoji}/@me"
                                    async with session.put(react_url, headers=headers) as resp:
                                        if resp.status in (200, 204):
                                            pass
                                        else:
                                            print(f"[MultiReact] {alias} failed: {resp.status}")
                                    await asyncio.sleep(0.3)
                                except Exception as e:
                                    print(f"[MultiReact] {alias} error: {e}")
                                    await asyncio.sleep(1)
                            await asyncio.sleep(0.5)
                            
                    except asyncio.CancelledError:
                        print(f"[MultiReact] {alias} task cancelled")
                    except Exception as e:
                        print(f"[MultiReact] {alias} error: {e}")
            
            # Start workers
            for token_info in ctx.bot.token_pool:
                alias = token_info.get("alias", "unknown")
                task = asyncio.create_task(react_worker(token_info, target_channel_id, alias, emoji_list))
                ctx.bot.multireact_tasks[alias] = task
                await asyncio.sleep(0.5)
            
            await ctx.send(f" MultiReact started with {len(ctx.bot.token_pool)} token(s) in <#{target_channel_id}>")
        
        # ========== STOP MULTIREACT ==========
        @self.command(name='stopmultireact')
        async def stopmultireact(ctx):
            """Stop all multi-reaction tasks"""
            try:
                await ctx.message.delete()
            except:
                pass
            
            if not hasattr(ctx.bot, 'multireact_tasks') or not ctx.bot.multireact_tasks:
                await ctx.send(" No active multireact tasks.")
                return
            
            count = 0
            for alias, task in list(ctx.bot.multireact_tasks.items()):
                if not task.done():
                    task.cancel()
                    count += 1
            ctx.bot.multireact_tasks.clear()
            
            await ctx.send(f" Stopped {count} multireact task(s).")

        # ========== MULTISTAM ==========
        @self.command(name='multistam')
        async def multistam(ctx, channel_id: int = None, delay: float = 2.0, *, message: str = None):
            """Stam from all hosted tokens - .multistam [channel_id] [delay] <message>"""
            try:
                await ctx.message.delete()
            except:
                pass
            
            if not ctx.bot.token_pool:
                await ctx.send(" No tokens loaded. Use `.host <token>` first.")
                return
            
            target_channel_id = channel_id or ctx.channel.id
            if channel_id and not ctx.bot.get_channel(channel_id):
                await ctx.send(" Invalid channel ID!")
                return
            
            if not message:
                await ctx.send(" Usage: `.multistam [channel_id] [delay] <message>`")
                return
            
            # Cancel existing tasks
            if hasattr(ctx.bot, 'multistam_tasks'):
                for alias, task in list(ctx.bot.multistam_tasks.items()):
                    if not task.done():
                        task.cancel()
            ctx.bot.multistam_tasks = {}
            
            async def stam_worker(token_info, channel_id, alias, msg, delay):
                token = token_info["token"]
                headers = {"Authorization": token, "Content-Type": "application/json"}
                url = f"https://discord.com/api/v9/channels/{channel_id}/messages"
                
                async with aiohttp.ClientSession() as session:
                    try:
                        # Verify token
                        async with session.get("https://discord.com/api/v9/users/@me", headers=headers) as resp:
                            if resp.status != 200:
                                print(f"[MultiStam] {alias} token invalid: HTTP {resp.status}")
                                return
                            user_data = await resp.json()
                            print(f"[MultiStam] {alias} authenticated as {user_data['username']}")
                        
                        # Main loop - send message with counter
                        counter = 1
                        while True:
                            await asyncio.sleep(0.1)
                            
                            # Add counter to message
                            msg_with_count = f"{msg} ({counter})"
                            payload = {"content": msg_with_count}
                            
                            try:
                                async with session.post(url, json=payload, headers=headers) as resp:
                                    if resp.status in (200, 204):
                                        pass
                                    else:
                                        print(f"[MultiStam] {alias} send failed: {resp.status}")
                                        await asyncio.sleep(5)
                            except Exception as e:
                                print(f"[MultiStam] {alias} error: {e}")
                                await asyncio.sleep(5)
                            
                            counter += 1
                            await asyncio.sleep(delay)
                            
                    except asyncio.CancelledError:
                        print(f"[MultiStam] {alias} task cancelled")
                    except Exception as e:
                        print(f"[MultiStam] {alias} error: {e}")
            
            # Start workers
            for token_info in ctx.bot.token_pool:
                alias = token_info.get("alias", "unknown")
                task = asyncio.create_task(stam_worker(token_info, target_channel_id, alias, message, delay))
                ctx.bot.multistam_tasks[alias] = task
                await asyncio.sleep(0.5)
            
            await ctx.send(f" MultiStam started with {len(ctx.bot.token_pool)} token(s) in <#{target_channel_id}> with {delay}s delay")
        
        # ========== STOP MULTISTAM ==========
        @self.command(name='stopmultistam')
        async def stopmultistam(ctx):
            """Stop all multi-stam tasks"""
            try:
                await ctx.message.delete()
            except:
                pass
            
            if not hasattr(ctx.bot, 'multistam_tasks') or not ctx.bot.multistam_tasks:
                await ctx.send(" No active multistam tasks.")
                return
            
            count = 0
            for alias, task in list(ctx.bot.multistam_tasks.items()):
                if not task.done():
                    task.cancel()
                    count += 1
            ctx.bot.multistam_tasks.clear()
            
            await ctx.send(f" Stopped {count} multistam task(s).")

        # ========== MULTICOUNT ==========
        @self.command(name='multicount')
        async def multicount(ctx, channel_id: int = None, start: int = 1, stop: int = 100):
            """Count from all hosted tokens - .multicount [channel_id] <start> <stop>"""
            try:
                await ctx.message.delete()
            except:
                pass
            
            if not ctx.bot.token_pool:
                await ctx.send(" No tokens loaded. Use `.host <token>` first.")
                return
            
            target_channel_id = channel_id or ctx.channel.id
            if channel_id and not ctx.bot.get_channel(channel_id):
                await ctx.send(" Invalid channel ID!")
                return
            
            if start > stop:
                await ctx.send(" Start value must be less than stop value.")
                return
            
            # Cancel existing tasks
            if hasattr(ctx.bot, 'multicount_tasks'):
                for alias, task in list(ctx.bot.multicount_tasks.items()):
                    if not task.done():
                        task.cancel()
            ctx.bot.multicount_tasks = {}
            
            async def count_worker(token_info, channel_id, alias, start_num, stop_num, token_index, total_tokens):
                token = token_info["token"]
                headers = {"Authorization": token, "Content-Type": "application/json"}
                url = f"https://discord.com/api/v9/channels/{channel_id}/messages"
                
                async with aiohttp.ClientSession() as session:
                    try:
                        # Verify token
                        async with session.get("https://discord.com/api/v9/users/@me", headers=headers) as resp:
                            if resp.status != 200:
                                print(f"[MultiCount] {alias} token invalid: HTTP {resp.status}")
                                return
                            user_data = await resp.json()
                            print(f"[MultiCount] {alias} authenticated as {user_data['username']}")
                        
                        # Each token starts from a different number
                        # Token 1: 1, 4, 7, 10...
                        # Token 2: 2, 5, 8, 11...
                        # Token 3: 3, 6, 9, 12...
                        offset = token_index  # 0-based index
                        step = total_tokens
                        
                        current = start_num + offset
                        
                        while current <= stop_num:
                            await asyncio.sleep(0.1)
                            
                            payload = {"content": str(current)}
                            
                            try:
                                async with session.post(url, json=payload, headers=headers) as resp:
                                    if resp.status in (200, 204):
                                        pass
                                    else:
                                        print(f"[MultiCount] {alias} send failed: {resp.status}")
                                        await asyncio.sleep(3)
                            except Exception as e:
                                print(f"[MultiCount] {alias} error: {e}")
                                await asyncio.sleep(3)
                            
                            current += step
                            await asyncio.sleep(0.8)  # Delay between numbers
                            
                    except asyncio.CancelledError:
                        print(f"[MultiCount] {alias} task cancelled")
                    except Exception as e:
                        print(f"[MultiCount] {alias} error: {e}")
            
            # Start workers with staggered numbering
            total_tokens = len(ctx.bot.token_pool)
            for i, token_info in enumerate(ctx.bot.token_pool):
                alias = token_info.get("alias", "unknown")
                task = asyncio.create_task(count_worker(token_info, target_channel_id, alias, start, stop, i, total_tokens))
                ctx.bot.multicount_tasks[alias] = task
                await asyncio.sleep(0.3)
            
            await ctx.send(f" MultiCount started with {len(ctx.bot.token_pool)} token(s) in <#{target_channel_id}> from {start} to {stop}")
        
        # ========== STOP MULTICOUNT ==========
        @self.command(name='stopmulticount')
        async def stopmulticount(ctx):
            """Stop all multi-count tasks"""
            try:
                await ctx.message.delete()
            except:
                pass
            
            if not hasattr(ctx.bot, 'multicount_tasks') or not ctx.bot.multicount_tasks:
                await ctx.send(" No active multicount tasks.")
                return
            
            count = 0
            for alias, task in list(ctx.bot.multicount_tasks.items()):
                if not task.done():
                    task.cancel()
                    count += 1
            ctx.bot.multicount_tasks.clear()
            
            await ctx.send(f" Stopped {count} multicount task(s).")
        
        # ========== SNIPE ==========
        @self.command(name='snipeset')
        async def snipeset(ctx):
            ctx.bot.snipe_enabled.add(ctx.channel.id)
            await ctx.send("Snipe enabled in this chat")
        
        @self.command(name='snipestop')
        async def snipestop(ctx):
            if ctx.channel.id in ctx.bot.snipe_enabled:
                ctx.bot.snipe_enabled.remove(ctx.channel.id)
                await ctx.send("Snipe disabled here")
        
        # ========== DATE ==========
        @self.command(name='date')
        async def date(ctx):
            now = datetime.now()
            date_str = now.strftime("%A, %B %d, %Y")
            await ctx.send(f"**Today is** {date_str}")
        
        # ========== UPTIME ==========
        @self.command(name='uptime')
        async def uptime(ctx):
            uptime_seconds = int(time.time() - ctx.bot.start_time)
            hours = uptime_seconds // 3600
            minutes = (uptime_seconds % 3600) // 60
            seconds = uptime_seconds % 60
            await ctx.send(f"Uptime: {hours}h {minutes}m {seconds}s")

        @self.command(name='setprefix')
        async def setprefix(ctx, new_prefix: str):
            """Change the bot's command prefix"""
            try:
                await ctx.message.delete()
            except:
                pass
            
            self.command_prefix = new_prefix
            
            PREFIX = new_prefix
            
            await ctx.send(f" Prefix changed to `{new_prefix}`")

        @self.command(name='prefix')
        async def show_prefix(ctx):
            """Show current command prefix"""
            try:
                await ctx.message.delete()
            except:
                pass
            await ctx.send(f"Current prefix: `{self.command_prefix}`")
        
        # ========== PING ==========
        @self.command(name='ping')
        async def ping(ctx):
            await ctx.send(f"{round(ctx.bot.latency * 1000)}ms")
        
        # ========== HOSTALL ==========
        @self.command(name='hostall')
        async def hostall(ctx, token: str):
            """Host a token as a FULLY INDEPENDENT bot instance"""
            await ctx.message.delete()
            
            if token in hosted_bots_set:
                await ctx.send("Token is already hosted.", delete_after=5)
                return
            
            headers = {"Authorization": token}
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.get("https://discord.com/api/v9/users/@me", headers=headers) as resp:
                        if resp.status != 200:
                            await ctx.send(f"Invalid token (HTTP {resp.status})", delete_after=5)
                            return
                        user_data = await resp.json()
                        username = user_data.get("username", "unknown")
                except Exception as e:
                    await ctx.send(f"Error validating token: {e}", delete_after=5)
                    return
            
            try:
                new_bot = SupremeBot(token=token)
                hosted_bots.append(new_bot)
                hosted_bots_set.add(token)
                asyncio.create_task(new_bot.start(token))
                await ctx.send(f"**{username}** hosted successfully. Total hosted: {len(hosted_bots)}")
            except Exception as e:
                await ctx.send(f"Failed to host token: {e}", delete_after=5)
        
        # ========== UNHOSTALL ==========
        @self.command(name='unhostall')
        async def unhostall(ctx, username: str):
            await ctx.message.delete()
            
            bot_to_remove = None
            for bot in hosted_bots:
                try:
                    if bot.user and bot.user.name.lower() == username.lower():
                        bot_to_remove = bot
                        break
                except:
                    pass
            
            if not bot_to_remove:
                await ctx.send(f"No hosted token found with username: {username}", delete_after=5)
                return
            
            hosted_bots.remove(bot_to_remove)
            hosted_bots_set.remove(bot_to_remove.token)
            await bot_to_remove.close()
            await ctx.send(f"**{username}** unhosted successfully.")
        
        # ========== ALLTOKENS ==========
        @self.command(name='alltokens')
        async def alltokens(ctx):
            await ctx.message.delete()
            
            if not hosted_bots:
                await ctx.send("No tokens hosted.", delete_after=5)
                return
            
            msg = "**Hosted Tokens:**\n"
            for bot in hosted_bots:
                try:
                    if bot.user:
                        msg += f"• {bot.user.name}\n"
                except:
                    pass
            
            if len(msg) > 1900:
                for i in range(0, len(msg), 1900):
                    await ctx.send(msg[i:i+1900])
            else:
                await ctx.send(msg)
        
        # ========== MENU ==========
        @self.command(name='menu')
        async def menu(ctx, page: int = 1):
            """Display the command menu with colorful UI"""
            try:
                await ctx.message.delete()
            except:
                pass
            
            # ========== COMMANDS LIST - 4 PER PAGE, ONE COLOR PER PAGE ==========
            commands_list = [
                #  PAGE 1 - RED (Beef & Spam)
                (".ab", "<channel_id> <delay> <file.txt>", "Auto-beef in channel", "red"),
                (".ablow", "<channel_id> <delay> <file_name>", "Auto-beef (lowercase)", "red"),
                (".abstop", "", "Stop all auto-beef", "red"),
                (".spam", "<message>", "Spam in current channel", "red"),
                
                #  PAGE 2 - RED (Spam & Purge)
                (".stopspam", "", "Stop all spam", "red"),
                (".spamall", "<message>", "Spam with all tokens", "red"),
                (".purge", "<amount> [channel_id]", "Delete your messages", "red"),
                (".aball", "[channel_id] [wordlist]", "Beef with all tokens", "red"),
                
                #  PAGE 3 - RED (More Red Commands)
                (".aballstop", "", "Stop all token beef", "red"),
                (".joinall", "<invite>", "Join server with all tokens", "red"),
                (".vcspam", "<vc_id> <loops>", "Voice channel spam", "red"),
                (".nuke", "<server_id>", "Nuke a server", "red"),
                
                #  PAGE 4 - GREEN (Wordlists & Reactions)
                (".wordlist", "<name>", "Load a wordlist", "green"),
                (".wordlists", "", "List all wordlists", "green"),
                (".importwl", "<name>", "Import wordlist from file", "green"),
                (".react", "<emoji1> <emoji2> ...", "Auto-react to your messages", "green"),
                
                #  PAGE 5 - GREEN (Reactions & Snipe)
                (".stopreact", "", "Stop auto-react", "green"),
                (".snipeset", "", "Enable snipe in channel", "green"),
                (".snipestop", "", "Disable snipe in channel", "green"),
                (".pack", "<channel_id> <times> <lines> <type>", "Generate AI packs", "green"),
                
                #  PAGE 6 - GREEN (Auto Paste & Stream)
                (".autopaste", "<channel_id> <delay> <msg>", "Auto-paste in channel", "green"),
                (".autopastelist", "<channel_id>", "List auto-paste messages", "green"),
                (".autopasteremove", "<channel_id> <index>", "Remove auto-paste", "green"),
                (".stopautopaste", "<channel_id>", "Stop auto-paste", "green"),
                
                #  PAGE 7 - CYAN (Stream & Utilities)
                (".stream", "Title1,Title2,...", "Streaming status rotation", "cyan"),
                (".streamend", "", "Stop streaming", "cyan"),
                (".archive", "[channel_id] [limit]", "Archive channel messages", "cyan"),
                (".upload", "<url>", "Upload file from URL", "cyan"),
                
                #  PAGE 8 - CYAN (More Utilities)
                (".linkgen", "<name>", "Generate random links", "cyan"),
                (".saveinvite", "<invite_code>", "Save server invite", "cyan"),
                (".rejoinall", "", "Rejoin all saved servers", "cyan"),
                (".updateproxies", "", "Update proxy list", "cyan"),
                
                #  PAGE 9 - CYAN (Stam & Auto-Reply)
                (".stam", "<channel_id> <delay> <msg>", "Stam in channel", "cyan"),
                (".stamlist", "<channel_id>", "List stam messages", "cyan"),
                (".stamremove", "<channel_id> <index>", "Remove stam", "cyan"),
                (".stopstam", "<channel_id>", "Stop stam", "cyan"),
                
                #  PAGE 10- ORANGE (Auto-Reply)
                (".ar", "@user <channel_id> <reply>", "Auto-reply to user", "orange"),
                (".sar", "[@user]", "Stop auto-reply", "orange"),
                (".ar2", "@user <lines> <message>", "Flood user with messages", "orange"),
                (".pic", "<query>", "Search for image", "orange"),
                
                #  PAGE 11 - ORANGE (Counting & GC)
                (".autocount", "<channel_id> <start> [end]", "Auto-count in channel", "orange"),
                (".count", "<channel_id> <start>", "Countdown in channel", "orange"),
                (".stopac", "", "Stop counting", "orange"),
                (".gcname", "<channel_id> <delay> <name>", "Change GC name", "orange"),
                
                #  PAGE 12 - ORANGE (GC & Token Management)
                (".stopgc", "", "Stop GC name changer", "orange"),
                (".lockgc", "<channel_id> <name>", "Lock GC name", "orange"),
                (".host", "<token>", "Add token to pool", "orange"),
                (".hostall", "<token>", "Host full bot instance", "orange"),
                
                #  PAGE 13 - YELLOW (Token Management)
                (".unhostall", "<username>", "Unhost a token", "yellow"),
                (".alltokens", "", "List all hosted tokens", "yellow"),
                (".checktoken", "<token>", "Validate a token", "yellow"),
                (".setprefix", "<new_prefix>", "Change command prefix", "yellow"),
                
                #  PAGE 14 - YELLOW (Settings & Info)
                (".prefix", "", "Show current prefix", "yellow"),
                (".ping", "", "Check bot latency", "yellow"),
                (".uptime", "", "Show bot uptime", "yellow"),
                (".date", "", "Show current date", "yellow"),
                
                #  PAGE 15 - YELLOW (multi cmd)
                (".multireact", "<channel_id> <emoji1> <emoji2>", "React with all tokens", "yellow"),
                (".stopmultireact", "", "Stop all multireact", "yellow"),
                (".multistam", "<channel_id> <delay> <msg>", "Stam with all tokens", "yellow"),
                (".stopmultistam", "", "Stop all multistam", "yellow"),

                # PAGE 16 - PURPLE (multi cmd 2)
                (".multicount", "<channel_id> <start> <stop>", "Count with all tokens", "purple"),
                (".stopmulticount", "", "Stop all multicount", "purple"),
                (".multistream", "s,u,p,r,e,m,e", "Multiple streams at once", "purple"),
                (".stopmultistream", "", "Stop multi-streaming", "purple"),
            ]
            
            per_page = 4
            total_pages = (len(commands_list) + per_page - 1) // per_page
            
            if page < 1 or page > total_pages:
                page = 1
            
            self.current_page = page
            
            start = (page - 1) * per_page
            end = min(start + per_page, len(commands_list))
            page_cmds = commands_list[start:end]
            
            # Build menu
            box = f"```ansi\n"
            box += f"\u001b[1;37m Page {page}/{total_pages} | Tokens: {len(self.token_pool)} | Prefix: {self.command_prefix}\n | Supreme Regime. \n"
            box += f"\u001b[1;36m{'═' * 70}\n\n"
            
            # Color mapping
            COLORS = {
                "red": "\u001b[1;31m",
                "green": "\u001b[1;32m",
                "cyan": "\u001b[1;36m",
                "orange": "\u001b[38;5;214m",
                "yellow": "\u001b[1;33m",
                "white": "\u001b[1;37m",
                "purple": "\u001b[38;5;129m",
            }

            # Command entries
            for i, (cmd, usage, desc, color_name) in enumerate(page_cmds, start=start + 1):
                color = COLORS.get(color_name, "\u001b[1;37m")  # Default to white
                
                box += f"  {color}{str(i).zfill(2)}. {cmd}"
                if usage:
                    box += f" \u001b[1;37m{usage}"
                box += f"\n"
                box += f"      \u001b[1;30m└─ {desc}\u001b[0m\n\n"
                
            
            #  SHORTER FOOTER
            box += f"\u001b[1;36m{'═' * 70}\n"
            box += f"\u001b[1;37m .n → Next | .p → Prev | .menu <page>\n"
            box += f"\u001b[1;36m{'═' * 70}\n"
            box += f"  Supreme/Arkel Tool\n"
            box += f"\u001b[1;36m{'═' * 70}\n```"
            
            #  TRUNCATE IF TOO LONG (Discord limit is 2000)
            if len(box) > 1990:
                box = box[:1980] + "\n```"
            
            await ctx.send(box)
    
        @self.command(name='n')
        async def next_page(ctx):
            # Get the menu command and invoke it
            menu_cmd = ctx.bot.get_command('menu')
            await ctx.invoke(menu_cmd, page=self.current_page + 1)

        @self.command(name='p')
        async def prev_page(ctx):
            menu_cmd = ctx.bot.get_command('menu')
            await ctx.invoke(menu_cmd, page=self.current_page - 1)

# ========== MAIN ENTRY ==========
async def main():
    if not TOKEN:
        print("Set TOKEN environment variable")
        return
    
    main_bot = SupremeBot(token=TOKEN)
    hosted_bots.append(main_bot)
    hosted_bots_set.add(TOKEN)
    
    try:
        await main_bot.start(TOKEN)
    except discord.LoginFailure:
        print("Invalid token. Exiting.")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"error : {e}")
