import discord
from discord.ext import commands
import asyncio
import pafy
from . import utils
from .utils import config, token, data_type, checks
from apiclient.discovery import build
from discord.opus import Encoder as OpusEncoder
import queue
from threading import Thread
from io import BytesIO
import json
import locale
import random
import re
from pymongo import ReturnDocument
import copy
import weakref

#==================================================================================================================================================

youtube_match = re.compile(r"(?:https?\:\/\/)?(?:www\.)?(?:youtube(?:-nocookie)?\.com\/\S*[^\w\s-]|youtu\.be\/)([\w-]{11})(?:[^\w\s-]|$)")
MAX_PLAYLIST_SIZE = 1000

#==================================================================================================================================================

class Buffer(queue.Queue):
    def get(self):
        with self.not_empty:
            if not self.queue:
                self.not_empty.wait()
            item = self.queue.popleft()
            self.not_full.notify()
            return item

    def _discard(self, number):
        with self.not_empty:
            item = None
            for i in range(min(number, len(self.queue))):
                item = self.queue.popleft()
                if item[0] == b"":
                    self.queue.append((b"", item[1]))
                    break
            self.not_full.notify()
            return item[1]

#==================================================================================================================================================

class FFmpegWithBuffer(discord.FFmpegPCMAudio):
    def __init__(self, *args, **kwargs):
        discord.FFmpegPCMAudio.__init__(self, *args, **kwargs)
        self._buffer = Buffer(config.BUFFER_SIZE)
        self.counter = 0
        self._is_running = True
        thread = Thread(target=self.read_buffer, args=())
        thread.daemon = True
        thread.start()

    def read(self):
        item = self._buffer.get()
        self.counter = item[1]
        return item[0]

    def read_buffer(self):
        counter = 0
        while self._is_running:
            chunk = self._stdout.read(OpusEncoder.FRAME_SIZE)
            counter += 1
            if len(chunk) != OpusEncoder.FRAME_SIZE:
                self._buffer.put((b"", counter))
                return
            self._buffer.put((chunk, counter))

    def fast_forward(self, number):
        return self._buffer._discard(number)

    def cleanup(self):
        self._is_running = False
        discord.FFmpegPCMAudio.cleanup(self)

#==================================================================================================================================================

class Song:
    __slots__ = ("requestor", "title", "url", "default_volume", "index", "duration", "music")

    def __init__(self, requestor, title, url, index=0):
        self.requestor = requestor
        self.title = utils.discord_escape(title)
        self.url = url
        self.default_volume = 1.0
        self.index = index
        self.duration = "00:00:00"
        self.music = None

    def raw_update(self):
        try:
            video = pafy.new(self.url)
        except OSError:
            return
        for a in video.audiostreams:
            if a.bitrate == "128k":
                audio = a
                break
        else:
            audio = video.getbestaudio()
        try:
            url = audio.url
        except AttributeError:
            url = video.streams[-1].url
        self.duration = video.duration
        self.music = discord.PCMVolumeTransformer(
            FFmpegWithBuffer(
                url,
                before_options="-hide_banner -loglevel panic -reconnect 1"
            ),
            volume=self.default_volume
        )

    def info(self):
        if self.music:
            second_elapsed = int(self.music.original.counter * 0.02)
        else:
            second_elapsed = 0
        return f"{self.title} ({second_elapsed//3600:02}:{second_elapsed%3600//60:02}:{second_elapsed%60:02} / {self.duration})"

    def time_elapsed(self):
        second_elapsed = int(self.music.original.counter * 0.02)
        return (second_elapsed//3600, second_elapsed%3600//60, second_elapsed%60)

    def to_dict(self):
        return {"requestor_id": getattr(self.requestor, "id", None), "title": self.title, "url": self.url, "index": self.index}

#==================================================================================================================================================

class MusicQueue:
    __slots__ = ("playlist_data", "guild_id", "playlist", "_iter", "_lock", "_not_empty", "_not_full", "next_index")

    def __init__(self, bot, guild_id, *, next_index):
        self.playlist_data = bot.db.music_playlist_data
        self.guild_id = guild_id
        self.playlist = []
        self._iter = iter(self.playlist)
        self._lock = asyncio.Lock()
        self._not_empty = asyncio.Condition(self._lock)
        self._not_full = asyncio.Condition(self._lock)
        self.next_index = next_index

    async def put(self, song):
        async with self._not_full:
            song.index = self.next_index
            self.next_index += 1
            self.playlist.append(song)
            await self.playlist_data.update_one(
                {
                    "guild_id": self.guild_id
                },
                {
                    "$set": {
                        "next_index": self.next_index
                    },
                    "$push": {
                        "playlist": song.to_dict()
                    }
                }
            )
            self._not_empty.notify()

    async def put_many(self, songs):
        if songs:
            async with self._not_full:
                for s in songs:
                    s.index = self.next_index
                    self.next_index += 1
                    self.playlist.append(s)
                await self.playlist_data.update_one(
                    {
                        "guild_id": self.guild_id
                    },
                    {
                        "$set": {
                            "next_index": self.next_index
                        },
                        "$push": {
                            "playlist": {
                                "$each": [s.to_dict() for s in songs]
                            }
                        }
                    }
                )
                self._not_empty.notify()

    async def get(self):
        async with self._not_empty:
            if not len(self.playlist):
                await self._not_empty.wait()
            song = self.playlist.pop(0)
            await asyncio.shield(self.playlist_data.update_one({"guild_id": self.guild_id}, {"$pop": {"playlist": -1}, "$set": {"current_song": song.to_dict()}}))
            return song

    async def delete(self, position):
        async with self._not_empty:
            song = self.playlist.pop(position)
            await self.playlist_data.update_one({"guild_id": self.guild_id}, {"$pull": {"playlist": {"index": song.index}}})
            return song

    async def purge(self):
        async with self._not_empty:
            self.playlist.clear()
            await self.playlist_data.update_one({"guild_id": self.guild_id}, {"$set": {"playlist": []}})

    def __getitem__(self, key):
        if isinstance(key, slice):
            raise TypeError("Don't slice music queue.")
        else:
            return self.playlist[key]

    def __iter__(self):
        return iter(self.playlist)

    def __bool__(self):
        return bool(self.playlist)

    def __len__(self):
        return len(self.playlist)

    def __contains__(self, item):
        return item in self.playlist

#==================================================================================================================================================

class MusicPlayer:
    __slots__ = ("bot", "guild", "queue", "current_song", "repeat", "channel", "player", "lock", "auto_info", "inactivity")

    def __init__(self, bot, guild, *, initial, next_index):
        self.bot = bot
        self.guild = guild
        self.queue = MusicQueue(bot, guild.id, next_index=next_index)
        self.current_song = None
        self.repeat = False
        self.channel = None
        self.player = None
        self.lock = asyncio.Lock()
        self.queue.playlist.extend((Song(guild.get_member(s["requestor_id"]), s["title"], s["url"], s["index"]) for s in initial))
        self.auto_info = None
        self.inactivity = data_type.Observer()

    def ready_to_play(self, channel):
        self.channel = channel
        self.player = weakref.ref(self.bot.loop.create_task(self.play_till_eternity()))

    async def skip(self):
        if self.guild.voice_client.is_playing():
            self.guild.voice_client.stop()
            await self.clear_current_song()

    async def leave_voice(self):
        self.repeat = False
        if self.guild.voice_client:
            await asyncio.shield(self.guild.voice_client.disconnect(force=True))

    async def clear_current_song(self):
        self.current_song = None
        await asyncio.shield(self.queue.playlist_data.update_one({"guild_id": self.guild.id}, {"$set": {"current_song": None}}))

    def cancel(self):
        try:
            self.player().cancel()
        except AttributeError:
            pass
        self.inactivity.assign(0)

    async def play_till_eternity(self):
        def next_part(e):
            if e:
                print(e)
            self.bot.loop.call_soon_threadsafe(play_next_song.set)

        play_next_song = asyncio.Event()
        cmd = self.bot.get_command("music info")
        voice = self.guild.voice_client
        self.inactivity.assign(None)

        while True:
            play_next_song.clear()
            if not self.current_song:
                try:
                    self.current_song = await asyncio.wait_for(self.queue.get(), 120, loop=self.bot.loop)
                except asyncio.TimeoutError:
                    await self.channel.send("No music? Time to sleep then. Yaaawwnnnn~~")
                    break
            await self.bot.loop.run_in_executor(None, self.current_song.raw_update)
            if self.current_song.music is None:
                await self.channel.send(f"**{self.current_song.title}** is not available.")
                await self.clear_current_song()
            else:
                voice.play(self.current_song.music, after=next_part)
                name = utils.discord_escape(getattr(self.current_song.requestor, "display_name", "<User left server>"))
                await self.channel.send(f"Playing **{self.current_song.title}** requested by {name}.")
                if self.auto_info:
                    new_msg = copy.copy(self.auto_info)
                    new_msg.author = self.current_song.requestor or new_msg.author
                    new_ctx = await self.bot.get_context(new_msg, cls=data_type.BelphegorContext)
                    await new_ctx.invoke(cmd)
                await play_next_song.wait()
                if not self.repeat:
                    await self.clear_current_song()

            for m in voice.channel.members:
                if not m.bot:
                    break
            else:
                try:
                    await self.bot.wait_for("voice_state_update", check=lambda m, b, a: a.channel.id==voice.channel.id and not m.bot, timeout=120)
                except asyncio.TimeoutError:
                    await self.channel.send("Heeey, anybody's listening? No? Then I'll go to sleep.")
                    break

        self.inactivity.assign(0)
        async with self.lock:
            await self.leave_voice()

#==================================================================================================================================================

class Music:
    '''
    Music is life.
    '''

    def __init__(self, bot):
        self.bot = bot
        self.playlist_data = bot.db.music_playlist_data
        self.music_players = {}
        locale.setlocale(locale.LC_ALL, '')
        self.youtube = build("youtube", "v3", developerKey=token.GOOGLE_CLIENT_API_KEY)
        self.yt_lock = asyncio.Lock()
        self.mp_lock = asyncio.Lock()

    def cleanup(self):
        for mp in self.music_players.values():
            mp.cancel()
            self.bot.create_task_and_count(mp.leave_voice())

    async def cleanup_when_inactive(self, music_player):
        try:
            inactivity = music_player.inactivity
            while True:
                inactivity.clear()
                timeout = inactivity.item
                await inactivity.wait(timeout=timeout)
        except asyncio.TimeoutError:
            self.music_players.pop(music_player.guild.id, None)

    async def get_music_player(self, guild):
        async with self.mp_lock:
            mp = self.music_players.get(guild.id)
            if not mp:
                mp_data = await self.playlist_data.find_one_and_update(
                    {"guild_id": guild.id},
                    {"$setOnInsert": {"guild_id": guild.id, "next_index": 0, "playlist": [], "current_song": None}},
                    return_document=ReturnDocument.AFTER,
                    upsert=True
                )
                mp = MusicPlayer(self.bot, guild, initial=mp_data["playlist"], next_index=mp_data["next_index"])
                cur_song = mp_data.get("current_song")
                if cur_song:
                    mp.current_song = Song(guild.get_member(cur_song["requestor_id"]), cur_song["title"], cur_song["url"], cur_song["index"])
                self.music_players[guild.id] = mp
                self.bot.loop.create_task(self.cleanup_when_inactive(mp))
            if not mp.channel:
                mp.inactivity.assign(120)
            return mp

    @commands.group(aliases=["m"])
    @checks.guild_only()
    async def music(self, ctx):
        '''
            `>>music`
            Base command. Does nothing by itself, but with subcommands can be used to play music.
        '''
        if ctx.invoked_subcommand is None:
            pass

    @music.command(aliases=["j"])
    async def join(self, ctx):
        '''
            `>>music join`
            Have {0} join the current voice channel you are in and play everything in queue.
            May or may not bug out when the connection is unstable. If that happens, try move her to another channel.
        '''
        try:
            voice_channel = ctx.author.voice.channel
        except AttributeError:
            msg = await ctx.send("You are not in a voice channel. Try joining one, I'm waiting.")
            try:
                member, before, after = await self.bot.wait_for("voice_state_update", check=lambda m, b, a: m.id==ctx.author.id and a.channel.guild.id==ctx.guild.id, timeout=120)
            except asyncio.TimeoutError:
                return msg.edit("So you don't want to listen to music? Great, I don't have to work then!")
            else:
                voice_channel = after.channel
                await msg.delete()

        music_player = await self.get_music_player(ctx.guild)
        async with music_player.lock:
            if ctx.voice_client:
                await ctx.send("I am already in a voice channel.")
            else:
                msg = await ctx.send("Connecting...")
                try:
                    await voice_channel.connect(timeout=20, reconnect=False)
                except asyncio.TimeoutError:
                    return await msg.edit(content="Cannot connect to voice. Try joining other voice channel.")

                music_player.ready_to_play(ctx.channel)
                await msg.edit(content=f"{self.bot.user.display_name} joined {voice_channel.name}.")

    @music.command(aliases=["l"])
    async def leave(self, ctx):
        '''
            `>>music leave`
            Have {0} leave voice channel.
        '''
        music_player = await self.get_music_player(ctx.guild)
        async with music_player.lock:
            try:
                name = ctx.voice_client.channel.name
            except AttributeError:
                await ctx.send(f"{self.bot.user.display_name} is not in any voice channel.")
            else:
                music_player.cancel()
                await music_player.leave_voice()
                await ctx.send(f"{self.bot.user.display_name} left {name}.")

    def youtube_search(self, name, type="video"):
        search_response = self.youtube.search().list(q=name, part="id,snippet", type=type, maxResults=10).execute()
        results = []
        for search_result in search_response.get("items", None):
            results.append(search_result)
            if len(results) > 4:
                break
        return results

    def current_queue_info(self, music_player):
        try:
            if music_player.voice_client.is_playing():
                state = "Playing"
            else:
                state = "Paused"
        except:
            state = "Stopped"
        try:
            current_song_info = music_player.current_song.info()
        except AttributeError:
            current_song_info = ""
        if music_player.queue:
            return utils.embed_page_format(
                music_player.queue, 10, separator="\n\n",
                title=f"({state}) {current_song_info}",
                description=lambda i, x: f"`{i+1}.` **[{x.title}]({x.url})**",
                colour=discord.Colour.green(),
                thumbnail_url="http://i.imgur.com/HKIOv84.png"
                )
        else:
            return [discord.Embed(title=f"({state}) {current_song_info}", colour=discord.Colour.green())]

    @music.command(aliases=["q"])
    async def queue(self, ctx, *, name=None):
        '''
            `>>music queue <optional: name>`
            Search Youtube for a song and put it in queue.
            If no name is provided, the current queue is displayed instead.
            Queue is server-specific.
        '''
        music_player = await self.get_music_player(ctx.guild)
        if not name:
            embeds = self.current_queue_info(music_player)
            return await ctx.embed_page(embeds)
        if 1 + len(music_player.queue) > MAX_PLAYLIST_SIZE:
            return await ctx.send("Too many entries.")
        async with ctx.typing():
            results = await self.bot.run_in_lock(self.yt_lock, self.youtube_search, name)
            stuff = "\n\n".join([
                f"`{i+1}:` **[{utils.discord_escape(v['snippet']['title'])}](https://youtu.be/{v['id']['videoId']})**\n      By: {v['snippet']['channelTitle']}"
                for i,v in enumerate(results)
            ])
            embed = discord.Embed(title="\U0001f3b5 Video search result: ", description=f"{stuff}\n\n`<>:` cancel", colour=discord.Colour.green())
            embed.set_thumbnail(url="http://i.imgur.com/HKIOv84.png")
            await ctx.send(embed=embed)
            index = await ctx.wait_for_choice(max=len(results))
            if index is None:
                return
            else:
                result = results[index]
            title = result["snippet"]["title"]
            await music_player.queue.put(Song(ctx.message.author, title, f"https://youtu.be/{result['id']['videoId']}"))
            await ctx.send(f"Added **{title}** to queue.")

    @music.command(aliases=["s"])
    async def skip(self, ctx):
        '''
            `>>music skip`
            Skip current song.
        '''
        music_player = await self.get_music_player(ctx.guild)
        await music_player.skip()

    @music.command(aliases=["v"])
    async def volume(self, ctx, vol: int):
        '''
            `>>music volume <value>`
            Set volume of current song. Volume must be an integer between 0 and 200.
            Default volume is 100.
        '''
        music_player = await self.get_music_player(ctx.guild)
        if 0 <= vol <= 200:
            if music_player.current_song:
                music_player.current_song.default_volume = vol / 100
                music_player.current_song.music.volume = vol / 100
                await ctx.send(f"Volume for current song has been set to {vol}%.")
            else:
                await ctx.send("No song is currently playing.")
        else:
            await ctx.send("Volume must be between 0 and 200.")

    @music.command(aliases=["r"])
    async def repeat(self, ctx):
        '''
            `>>music repeat`
            Toggle repeat mode.
            The current song will be repeated indefinitely during repeat mode.
        '''
        music_player = await self.get_music_player(ctx.guild)
        if music_player.repeat:
            music_player.repeat = False
            await ctx.send("Repeat mode has been turned off.")
        else:
            music_player.repeat = True
            await ctx.send("Repeat mode has been turned on.")

    @music.command(aliases=["d"])
    async def delete(self, ctx, position: int):
        '''
            `>>music delete <position>`
            Delete a song from queue.
        '''
        music_player = await self.get_music_player(ctx.guild)
        queue = music_player.queue
        position -= 1
        if 0 <= position < len(queue):
            title = queue[position].title
            sentences = {
                "initial":  "Delet this?",
                "yes":      f"Deleted **{title}** from queue.",
                "no":       "Cancelled deleting.",
                "timeout":  "Timeout, cancelled deleting."
            }
            check = await ctx.yes_no_prompt(sentences)
            if check:
                await queue.delete(position)
        else:
            await ctx.send("Position out of range.")

    @music.command()
    async def purge(self, ctx):
        '''
            `>>music purge`
            Purge all songs from queue.
        '''
        music_player = await self.get_music_player(ctx.guild)
        sentences = {
            "initial":  f"Purge queue?",
            "yes":      "Queue purged.",
            "no":       "Cancelled purging.",
            "timeout":  "Timeout, cancelled purging."
        }
        check = await ctx.yes_no_prompt(sentences)
        if check:
            await music_player.queue.purge()

    @music.command()
    async def export(self, ctx, *, name="playlist"):
        '''
            `>>music export <optional: name>`
            Export current queue to a JSON file.
            If no name is provided, default name `playlist` is used instead.
        '''
        music_player = await self.get_music_player(ctx.guild)
        jsonable = []
        if music_player.current_song:
            jsonable.append({"title": music_player.current_song.title, "url": music_player.current_song.url})
        for song in music_player.queue:
            jsonable.append({"title": song.title, "url": song.url})
        bytes_ = json.dumps(jsonable, indent=4, ensure_ascii=False).encode("utf-8")
        await ctx.send(file=discord.File(bytes_, f"{name}.json"))

    @music.command(name="import")
    async def music_import(self, ctx):
        '''
            `>>music import`
            Import JSON playlist file to queue.
        '''
        music_player = await self.get_music_player(ctx.guild)
        msg = ctx.message
        if not msg.attachments:
            await msg.add_reaction("\U0001f504")
            try:
                msg = await self.bot.wait_for("message", check=lambda m:m.author.id==ctx.author.id and m.attachments, timeout=120)
            except asyncio.TimeoutError:
                return
            finally:
                try:
                    await ctx.message.clear_reactions()
                except:
                    pass
        bytes_ = BytesIO()
        await msg.attachments[0].save(bytes_)
        playlist = json.loads(bytes_.getvalue().decode("utf-8"))
        if isinstance(playlist, list):
            if len(playlist) + len(music_player.queue) > MAX_PLAYLIST_SIZE:
                return await ctx.send("Too many entries.")
        try:
            await music_player.queue.put_many([Song(msg.author, s["title"], s["url"]) for s in playlist])
            await ctx.send(f"Added {len(playlist)} songs to queue.")
        except:
            await ctx.send("Wrong format for imported file.")

    def youtube_playlist_items(self, message, playlist_id):
        results = []
        playlist_items = self.youtube.playlistItems().list(playlistId=playlist_id, part="snippet", maxResults=50).execute()
        for song in playlist_items.get("items", None):
            if song["snippet"]["title"] in ("Deleted video", "Private video"):
                continue
            else:
                results.append(Song(message.author, song["snippet"]["title"], f"https://youtu.be/{song['snippet']['resourceId']['videoId']}"))
        while playlist_items.get("nextPageToken", None):
            playlist_items = self.youtube.playlistItems().list(playlistId=playlist_id, part="snippet", maxResults=50, pageToken=playlist_items["nextPageToken"]).execute()
            for song in playlist_items.get("items", None):
                if song["snippet"]["title"] in ("Deleted video", "Private video"):
                    continue
                else:
                    results.append(Song(message.author, song["snippet"]["title"], f"https://youtu.be/{song['snippet']['resourceId']['videoId']}"))
        return results

    @music.command(aliases=["p"])
    async def playlist(self, ctx, *, name=None):
        '''
            `>>music playlist <optional: -r or -random flag> <optional: name>`
            Search Youtube for a playlist and put it in queue.
            If random flag is provided then the playlist is put in in random order.
            If no name is provided, the current queue is displayed instead.
        '''
        music_player = await self.get_music_player(ctx.guild)
        if not name:
            embeds = self.current_queue_info(music_player)
            return await ctx.embed_page(embeds)
        if name.startswith("-random "):
            shuffle = True
            name = name[8:]
        elif name.startswith("-r "):
            shuffle = True
            name = name[3:]
        else:
            shuffle = False
        results = await self.bot.run_in_lock(self.yt_lock, self.youtube_search, name, "playlist")
        stuff = "\n\n".join([
            f"`{i+1}:` **[{utils.discord_escape(p['snippet']['title'])}](https://www.youtube.com/playlist?list={p['id']['playlistId']})**\n      By: {p['snippet']['channelTitle']}"
            for i,p in enumerate(results)
        ])
        embed = discord.Embed(title="\U0001f3b5 Playlist search result: ", description=f"{stuff}\n\n`<>:` cancel", colour=discord.Colour.green())
        embed.set_thumbnail(url="http://i.imgur.com/HKIOv84.png")
        await ctx.send(embed=embed)
        index = await ctx.wait_for_choice(max=len(results))
        if index is None:
            return
        else:
            result = results[index]
        async with ctx.typing():
            items = await self.bot.run_in_lock(self.yt_lock, self.youtube_playlist_items, ctx.message, result["id"]["playlistId"])
            if len(items) + len(music_player.queue) > MAX_PLAYLIST_SIZE:
                return await ctx.send("Too many entries.")
            if shuffle:
                random.shuffle(items)
                add_text = " in random position"
            else:
                add_text = ""
            await music_player.queue.put_many(items)
            await ctx.send(f"Added {len(items)} songs to queue{add_text}.")

    def youtube_video_info(self, url):
        video_id = youtube_match.match(url).group(1)
        result = self.youtube.videos().list(part='snippet,contentDetails,statistics', id=video_id).execute()
        video = result["items"][0]
        return video

    @music.command(aliases=["i"])
    @commands.cooldown(rate=1, per=5, type=commands.BucketType.user)
    async def info(self, ctx, stuff="0"):
        '''
            `>>music info <optional: either queue position or youtube link>`
            Display video info.
            If no argument is provided, the currently playing song is used instead.
        '''
        try:
            position = int(stuff)
        except:
            url = stuff.strip("<>")
        else:
            music_player = await self.get_music_player(ctx.guild)
            position -= 1
            if position < 0:
                song = music_player.current_song
                if not song:
                    return await ctx.send("No song is currently playing.")
            elif 0 < position <= len(music_player.queue):
                song = music_player.queue[position]
            else:
                return await ctx.send("Position out of range.")
            url = song.url
        video = await self.bot.run_in_lock(self.yt_lock, self.youtube_video_info, url)
        snippet = video["snippet"]
        description = utils.unifix(snippet.get("description", "None")).strip()
        description_page = utils.split_page(description, 500)
        max_page = len(description_page)
        embeds = []
        for index, desc in enumerate(description_page):
            embed = discord.Embed(title=f"\U0001f3b5 {snippet['title']}", url=url, colour=discord.Colour.green())
            embed.set_thumbnail(url="http://i.imgur.com/HKIOv84.png")
            embed.add_field(name="Uploader", value=f"[{snippet['channelTitle']}](https://www.youtube.com/channel/{snippet['channelId']})")
            embed.add_field(name="Date", value=snippet["publishedAt"][:10])
            embed.add_field(name="Duration", value=f"\U0001f552 {video['contentDetails'].get('duration', '0s')[2:].lower()}")
            embed.add_field(name="Views", value=f"\U0001f441 {int(video['statistics'].get('viewCount', 0)):n}")
            embed.add_field(name="Likes", value=f"\U0001f44d {int(video['statistics'].get('likeCount', 0)):n}")
            embed.add_field(name="Dislikes", value=f"\U0001f44e {int(video['statistics'].get('dislikeCount', 0)):n}")
            embed.add_field(name="Description", value=f"{desc}\n\n(Page {index+1}/{max_page})", inline=False)
            for key in ("maxres", "standard", "high", "medium", "default"):
                value = snippet["thumbnails"].get(key, None)
                if value is not None:
                    embed.set_image(url=value["url"])
                    break
            embeds.append(embed)
        await ctx.embed_page(embeds)

    @music.command(aliases=["t"])
    async def toggle(self, ctx):
        '''
            `>>music toggle`
            Toggle play/pause.
            Should not pause for too long (hours), or else Youtube would complain.
        '''
        music_player = await self.get_music_player(ctx.guild)
        vc = ctx.voice_client
        if vc:
            if vc.is_paused():
                vc.resume()
                await ctx.send("Resumed playing.")
            elif vc.is_playing():
                vc.pause()
                await ctx.send("Paused.")

    @music.command(aliases=["f"])
    async def forward(self, ctx, seconds: int=10):
        '''
            `>>music forward <optional: seconds>`
            Fast forward. The limit is 59 seconds.
            If no argument is provided, fast forward by 10 seconds.
        '''
        music_player = await self.get_music_player(ctx.guild)
        song = music_player.current_song
        if song:
            if ctx.voice_client:
                if ctx.voice_client.is_playing():
                    if 0 < seconds < 60:
                        tbefore = song.time_elapsed()
                        safter = int(song.music.original.fast_forward(seconds*50) * 0.02)
                        tafter = (safter//3600, safter%3600//60, safter%60)
                        await ctx.send(f"Forward from {tbefore[0]:02}:{tbefore[1]:02}:{tbefore[2]:02} to {tafter[0]:02}:{tafter[1]:02}:{tafter[2]:02}.")
                    else:
                        await ctx.send("Fast forward time must be between 1 and 59 seconds.")
                    return

        await ctx.send("Nothing is playing right now, oi.")

    @music.command(aliases=["channel"])
    async def setchannel(self, ctx):
        '''
            `>>music setchannel`
            Set the current channel as song announcement channel.
        '''
        music_player = await self.get_music_player(ctx.guild)
        music_player.channel = ctx.channel
        await ctx.confirm()

    @music.command(aliases=["ai"])
    async def autoinfo(self, ctx):
        '''
            `>>music autoinfo`
            Automatic info display.
            Display channel is the current channel that this command is invoked in, and paging is associated with song requestor.
        '''
        music_player = await self.get_music_player(ctx.guild)
        music_player.auto_info = ctx.message
        await ctx.confirm()

    @music.command(aliases=["mi"])
    async def manualinfo(self, ctx):
        '''
            `>>music manualinfo`
            Manual info display.
        '''
        music_player = await self.get_music_player(ctx.guild)
        music_player.auto_info = None
        await ctx.confirm()

#==================================================================================================================================================

def setup(bot):
    bot.add_cog(Music(bot))
