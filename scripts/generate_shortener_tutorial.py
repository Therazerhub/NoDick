"""Regenerate the bundled generic sponsored-pass walkthrough (needs ffmpeg)."""
from pathlib import Path
import io
import subprocess
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SLIDES = [
    ('1-DAY PREMIUM', 'A quick walkthrough', ['Use your own Telegram account.', 'This is a general guide.', 'Provider pages may look different.']),
    ('STEP 1', 'Get your personal link', ['In the bot, choose', 'Get 1-Day Premium.', 'Then tap Open Sponsor Link.']),
    ('STEP 2', 'Follow the provider steps', ['Read the instructions on the page.', 'Wait for any required countdown.', 'Complete a CAPTCHA if requested.']),
    ('STAY SAFE', 'Do not install anything', ['Do not enter passwords or card details.', 'Do not enable browser notifications.', 'Close unrelated pop-ups.']),
    ('STEP 3', 'Return to Telegram', ['Use the final destination link.', 'Open the same bot and tap Start', 'if Telegram asks you to.']),
    ('ACTIVATED', '24 hours of Premium', ['The return link belongs to your account.', 'It expires after 30 minutes.', 'Redeem once for 24 hours of Premium.']),
]


def main():
    images = []
    for i, (label, title, lines) in enumerate(SLIDES):
        image = Image.new('RGB', (720, 1280), '#101015')
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((35, 60, 685, 1220), radius=30, outline='#383845', width=3)
        draw.text((70, 115), 'NoDick / Tutorial', font_size=27, fill='#a8a8b7')
        draw.text((70, 300), label, font_size=30, fill='#a5b4fc')
        # Keep titles wrapped without relying on a host-specific font.
        words = title.split(); rows = ['']
        for word in words:
            trial = (rows[-1] + ' ' + word).strip()
            if draw.textbbox((0,0), trial, font_size=52)[2] > 580:
                rows.append(word)
            else:
                rows[-1] = trial
        for j, row in enumerate(rows):
            draw.text((70, 370 + j*65), row, font_size=52, fill='#ffffff')
        for j, line in enumerate(lines):
            draw.text((70, 600 + j*68), line, font_size=26, fill='#c8c8d4')
        draw.text((70, 1110), f'{i+1} / {len(SLIDES)}', font_size=25, fill='#a8a8b7')
        stream = io.BytesIO(); image.save(stream, 'PNG'); images.append(stream.getvalue())
    output = ROOT / 'assets' / 'shortener-tutorial.mp4'
    output.parent.mkdir(exist_ok=True)
    result = subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
        '-framerate', '1/4', '-f', 'image2pipe', '-vcodec', 'png', '-i', 'pipe:0',
        '-vf', 'fps=24', '-t', '24', '-c:v', 'libx264', '-preset', 'fast',
        '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(output),
    ], input=b''.join(images), check=True)
    print(output)
    print(f'{output.stat().st_size} bytes')


if __name__ == '__main__':
    main()
