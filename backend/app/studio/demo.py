"""Scripted demo story.

The demo replays a short, original story through the *same tool executors* the
voice agent uses - so it exercises the real story engine, image generation and
playback with no microphone and no AssemblyAI session. The UI labels it
'scripted demo' so nobody mistakes it for a live voice session.
"""

from __future__ import annotations

from typing import Any, Dict, List

AMARA = {"name": "Amara", "description": "a curious young girl", "age": "about 9",
         "appearance": "dark skin, long black braids",
         "clothing": "yellow dress, brown sandals, red bracelet"}
KITO = {"name": "Kito", "description": "a small silver fox", "appearance": "silver fur, bright green eyes"}

STEPS: List[Dict[str, Any]] = [
    {"user": "Tell the story of a young girl who discovers a mysterious glowing tree in her village.",
     "agent": "Lovely. What's her name? Actually, let me start with Amara, and paint the first scenes.",
     "tool": "create_story", "args": {"title": "The Lantern Tree", "genre": "fairy tale", "tone": "wondrous",
                                         "visual_style": "warm hand-painted children's picture book illustration"}},
    {"user": "Amara lived in a small village under the mountains. One evening, she saw a soft glow between the pines.",
     "agent": "Lovely, painting that now. I'm picturing Amara with long braids and a yellow dress.",
     "tool": "add_story_content", "args": {
         "narration_text": "Amara lived in a small village under the mountains. One evening, she saw a soft glow between the pines.",
         "characters": [AMARA],
         "locations": [{"name": "Stonebridge village", "description": "a small mountain village of stone houses",
                        "visual_traits": ["stone houses with red roofs", "mountains behind", "pine trees"]}],
         "scenes": [
             {"narration": "Amara lived in a small village under the mountains.",
              "summary": "Amara's quiet mountain village at dusk", "setting": "Stonebridge village",
              "characters": ["Amara"], "emotion": "calm", "actions": ["standing at her doorstep"],
              "visual_prompt": "dusk over a small mountain village, Amara at the edge of the village looking toward the pines"},
             {"narration": "One evening, she saw a soft glow between the pines.",
              "summary": "Amara spots a glow in the pines", "setting": "the pine forest",
              "characters": ["Amara"], "emotion": "curious", "actions": ["pointing at a faint light"],
              "visual_prompt": "night falls, a faint glowing tree far between dark pines, Amara pointing at it"}]}},
    {"user": "She crept closer and found an enormous tree that glowed like a lantern. When she touched it, hundreds of tiny lights floated up into the sky.",
     "agent": "Beautiful. Painting the tree now.",
     "tool": "add_story_content", "args": {
         "narration_text": "She crept closer and found an enormous tree that glowed like a lantern. When she touched it, hundreds of tiny lights floated up into the sky.",
         "characters": [KITO],
         "scenes": [
             {"narration": "She crept closer and found an enormous tree that glowed like a lantern.",
              "summary": "The glowing lantern tree", "setting": "the clearing", "characters": ["Amara", "Kito"],
              "emotion": "awe", "actions": ["staring up at the glowing tree"],
              "visual_prompt": "an enormous glowing magical tree in a clearing, soft green light, Amara small in front of it with a silver fox"},
             {"narration": "When she touched it, hundreds of tiny lights floated up into the sky.",
              "summary": "Lights rise from the tree", "setting": "the clearing", "characters": ["Amara"],
              "emotion": "wonder", "actions": ["touching the bark"],
              "visual_prompt": "a giant glowing tree, Amara touching its trunk, hundreds of glowing lights drifting up into a starry night sky"}]}},
    {"user": "Make the tree blue and more magical.",
     "agent": "Done, repainting the tree scene with a blue, magical glow.",
     "tool": "regenerate_scene", "args": {"scene_number": 3,
                                          "revision_instruction": "make the tree blue and more magical, with glowing blue light"}},
    {"user": "The whole village woke and gathered around the tree, and the mountains have been bright every night since.",
     "agent": "What a lovely ending. Painting the last scene.",
     "tool": "add_story_content", "args": {
         "narration_text": "The whole village woke and gathered around the tree, and the mountains have been bright every night since.",
         "scenes": [{"narration": "The whole village woke and gathered around the tree, and the mountains have been bright every night since.",
                     "summary": "The village gathers under the tree", "setting": "Stonebridge village",
                     "characters": ["Amara", "Kito"], "emotion": "joy", "actions": ["celebrating together"],
                     "visual_prompt": "a joyful night, the whole village gathered under the great glowing blue tree, lights drifting over the mountains, warm and happy"}]}},
    {"user": "Play the story.", "agent": "Here it is.", "tool": "play_story", "args": {}},
]


def script() -> List[Dict[str, Any]]:
    return STEPS
