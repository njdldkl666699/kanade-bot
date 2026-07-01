from nonebot import on_command
from nonebot.matcher import Matcher


test_matcher = Matcher.new()

print(test_matcher)
print(test_matcher.__name__)
print(test_matcher.__class__)
print(test_matcher.__class__.__name__)
