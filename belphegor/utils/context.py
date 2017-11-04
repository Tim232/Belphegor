from discord import Embed
from discord.ext import commands
import re
import functools

def _do_nothing(obj):
    return obj

#==================================================================================================================================================

class BelphegorContext(commands.Context):
    async def confirm(self):
        await self.message.add_reaction("\u2705")

    async def deny(self):
        await self.message.add_reaction("\u274c")

    async def embed_page(self, embeds, *, timeout=60, target=None):
        _loop = self.bot.loop
        item = embeds[0]
        vertical = isinstance(item, list)
        if vertical:
            message = await self.send(embed=item[0])
            max_page = sum((len(p) for p in embeds))
            max_vertical = len(embeds)
            if max_vertical == 1:
                vertical = False
                embeds = item
        else:
            max_vertical = 1
            message = await self.send(embed=item)
            max_page = len(embeds)
        if max_page > 1:
            target = target or self.author
            current_page = 0
            if max_page > max_vertical:
                possible_reactions = ["\u23ee", "\u25c0", "\u25b6", "\u23ed"]
            else:
                possible_reactions = []
            if vertical:
                pool_index = 0
                pool = item
                max_page = len(pool)
                possible_reactions.extend(("\U0001f53c", "\U0001f53d", "\u274c"))
            else:
                pool = embeds
                possible_reactions.append("\u274c")
            for r in possible_reactions:
                _loop.create_task(message.add_reaction(r))

            async def rmv_rection(r, u):
                try:
                    await message.remove_reaction(r, u)
                except:
                    pass

            while True:
                try:
                    reaction, user = await self.bot.wait_for(
                        "reaction_add",
                        check=lambda r,u: u.id==target.id and r.emoji in possible_reactions and r.message.id==message.id,
                        timeout=timeout
                    )
                except:
                    try:
                        await message.clear_reactions()
                    finally:
                        return
                e = reaction.emoji
                if e == "\u25c0":
                    current_page = max(current_page-1, 0)
                elif e == "\u25b6":
                    current_page = min(current_page+1, max_page-1)
                elif e == "\u23ee":
                    current_page = max(current_page-10, 0)
                elif e == "\u23ed":
                    current_page = min(current_page+10, max_page-1)
                elif e == "\u274c":
                    try:
                        await message.clear_reactions()
                    finally:
                        return
                elif vertical:
                    if e == "\U0001f53c":
                        pool_index = max(pool_index-1, 0)
                        pool = embeds[pool_index]
                        max_page = len(pool)
                        current_page = min(current_page, max_page-1)
                    elif e == "\U0001f53d":
                        pool_index = min(pool_index+1, max_vertical-1)
                        pool = embeds[pool_index]
                        max_page = len(pool)
                        current_page = min(current_page, max_page-1)
                await message.edit(embed=pool[current_page])
                _loop.create_task(rmv_rection(reaction, user))

    async def yes_no_prompt(self, sentences, *, timeout=60, target=None, delete_mode=False):
        _loop = self.bot.loop
        message = await self.send(sentences["initial"])
        target = target or self.author
        possible_reactions = ("\u2705", "\u274c")
        for r in possible_reactions:
            _loop.create_task(message.add_reaction(r))
        try:
            reaction, user = await self.bot.wait_for(
                "reaction_add",
                check=lambda r,u: u.id==target.id and r.emoji in possible_reactions and r.message.id==message.id,
                timeout=timeout
            )
        except:
            result = False
            if not delete_mode:
                _loop.create_task(message.edit(content=sentences["timeout"]))
        else:
            if reaction.emoji == "\u2705":
                result = True
                if not delete_mode:
                    _loop.create_task(message.edit(content=sentences["yes"]))
            else:
                result = False
                if not delete_mode:
                    _loop.create_task(message.edit(content=sentences["no"]))
        if delete_mode:
            _loop.create_task(message.delete())
        else:
            try:
                _loop.create_task(message.clear_reactions())
            except:
                pass
        return result

    async def search(self, name, pool, *, cls=_do_nothing, atts=["id"], name_att, emoji_att=None, prompt=None):
        try:
            atts.remove("id")
            item_id = int(name)
        except:
            pass
        else:
            result = await pool.find_one({"id": item_id})
            if result:
                return cls(result)
            else:
                await self.send(f"Can't find {name} in database.")
                return None
        name = name.lower()
        regex = ".*?".join(map(re.escape, name.split()))
        cursor = pool.find({
            "$or": [
                {
                    att: {
                        "$regex": regex,
                        "$options": "i"
                    }
                } for att in atts
            ]
        })
        if prompt is False:
            async for item_data in cursor:
                if name in (item_data.get(att, "").lower() for att in atts):
                    break
            try:
                return cls(item_data)
            except:
                await self.send(f"Can't find {name} in database.")
                return None
        else:
            result = [cls(item_data) async for item_data in cursor]
            if not result:
                await self.send(f"Can't find {name} in database.")
                return None
            elif len(result) == 1 and not prompt:
                return result[0]
            embeds = []
            max_page = (len(result) - 1) // 10 + 1
            emojis = self.cog.emojis
            for index in range(0, len(result), 10):
                desc = "\n".join((
                    f"`{index+i+1}:` {emojis.get(getattr(item, emoji_att), '') if emoji_att else ''}{getattr(item, name_att)}"
                    for i, item in enumerate(result[index:index+10])
                ))
                embed = Embed(
                    title="Do you mean:",
                    description=f"{desc}\n\n(Page {index//10+1}/{max_page})"
                )
                embeds.append(embed)
            self.bot.loop.create_task(self.embed_page(embeds))
            msg = await self.bot.wait_for("message", check=lambda m: m.author.id==self.author.id)
            try:
                index = int(msg.content) - 1
            except:
                return None
            if index in range(len(result)):
                return result[index]
            else:
                return None