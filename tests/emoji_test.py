import emoji

e = "☺"
print(len(e))
print(emoji.is_emoji(e))
print(emoji.emoji_list(e))
print(emoji.EMOJI_DATA.get(e))
print(emoji.distinct_emoji_list(e))
print(list(m for m in e))
print([ord(c) for c in e])
print(e)
