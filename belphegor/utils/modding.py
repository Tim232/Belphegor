import discord
from discord.ext import commands
from . import format
import multidict

class MultiDict(multidict.MultiDict):
    def geteither(self, *keys, default=None):
        for key in keys:
            try:
                value = self.getone(key)
            except KeyError:
                continue
            else:
                return value
        else:
            return default

EMPTY = MultiDict()
_quotes = commands.view._quotes
_all_quotes = set((*_quotes.keys(), *_quotes.values()))
_delimiters = _all_quotes | set(("=",))

def _check_char(c):
    return c.isspace() or c in _delimiters

class KeyValue(commands.Converter):
    def __init__(self, conversion={}, *, escape=False, clean=True, multiline=True):
        self.escape = escape
        if clean:
            self.clean = format.clean_codeblock
        else:
            self.clean = str.strip
        c = {}
        for key, value in conversion.items():
            if isinstance(key, tuple):
                for k in key:
                    c[k] = value
            else:
                c[key] = value
        self.conversion = c
        self.multiline = multiline

    async def convert(self, ctx, argument):
        text = self.clean(argument)
        ret = MultiDict()

        async def resolve(key, value):
            if self.escape:
                value = value.encode("raw_unicode_escape").decode("unicode_escape")
            conv = self.conversion.get(key)
            if conv:
                value = await ctx.command.do_conversion(ctx, conv, value, key)
            ret.add(key, value)

        if self.multiline:
            for line in text.splitlines():
                line = line.strip()
                if line:
                    key, sep, value = line.partition("=")
                    if sep:
                        key, value = key.strip(), value.strip()
                    else:
                        key, value = "", key.strip()
                    await resolve(key, value)
        else:
            wi = format.split_iter(text, check=_check_char)
            key = ""
            prev_word = ""
            value = None

            while True:
                try:
                    word = next(wi)
                except StopIteration:
                    break

                if word == "=":
                    key = prev_word
                    value = ""
                elif word in _quotes:
                    quote_close = _quotes[word]
                    quote_words = []
                    escape = False
                    while True:
                        try:
                            w = next(wi)
                        except StopIteration:
                            raise commands.BadArgument("No closing quote.")
                        else:
                            if escape:
                                quote_words.append(w)
                                escape = False
                            elif w == quote_close:
                                value = "".join(quote_words)
                                print(value)
                                await resolve(key, value)
                                key = ""
                                prev_word = ""
                                value = None
                                break
                            else:
                                if w == "\\":
                                    escape = True
                                quote_words.append(w)
                elif not word.isspace():
                    if prev_word and not key and not value:
                        await resolve("", prev_word)
                    prev_word = word
                    if value is not None:
                        value = word
                    if key or value:
                        await resolve(key, value)
                        key = ""
                        prev_word = ""
                        value = None
            if prev_word:
                await resolve("", prev_word)

        return ret

class ExtraCommandFeatures:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.category = kwargs.pop("category", None)
        self.field = kwargs.pop("field", "Commands")
        self.paragraph = kwargs.pop("paragraph", 0)

class Command(ExtraCommandFeatures, commands.Command):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

class Group(ExtraCommandFeatures, commands.Group):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def command(self, **kwargs):
        def wrapper(func):
            cmd = command(**kwargs)(func)
            self.add_command(cmd)
            return cmd
        return wrapper

    def group(self, **kwargs):
        def wrapper(func):
            cmd = group(**kwargs)(func)
            self.add_command(cmd)
            return cmd
        return wrapper

def command(*, name=None, **kwargs):
    return commands.command(name, Command, **kwargs)

def group(*, name=None, **kwargs):
    return commands.command(name, Group, **kwargs)
