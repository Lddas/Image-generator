# Seedream + Seedance Generator

A small local app for BytePlus ModelArk image generation, Seedance video generation, and reusable sprite-sheet exports.

Generated images persist across page reloads and server restarts. Deleting an
image in the UI moves it to `outputs/.trash/` rather than permanently erasing
it. Deleting an attached image automatically removes it from the prompt.
Uploaded references are saved under `inputs/`, generated videos under `videos/`,
and sprite sheets plus JSON metadata under `sprites/`. These folders are created
automatically and ignored by Git.

“Edit image with SAM” uses fal.ai SAM2: select/upload an image, click an object,
choose the tampon (mask-edge expansion), then prompt an edit using that cutout.
Point and draggable bounding-box selection are supported. “Decompose to assets”
uses a vision model to locate elements and one Seedream Pro call per element;
“Element sheet” creates one white-background sheet in a single call.
The model selector offers fal.ai SAM2 (point or box) and SAM3 (box selection).

## Start

```bash
python3 server.py
```

Open <http://127.0.0.1:8080>.

## Repository layout

The repository should look like this:

```text
seedream-image-generator/
├── .gitignore
├── README.md
├── env                  # your real local keys; ignored by Git
├── env.example          # safe template committed to Git
├── index.html           # browser UI
├── server.py            # local server and Seedream API client
├── inputs/              # created automatically for uploaded input images
├── outputs/             # generated images
├── videos/              # generated Seedance MP4 files
├── sprites/             # sprite-sheet PNG and JSON metadata
└── segments/            # transparent fal.ai SAM2 selections
```

## Configuration

The server reads `env` from the repository root—the same folder as `server.py`.
It needs either `ARK_API_KEY` or `SEEDANCE_API_KEY`. SAM editing also needs
`FAL_KEY`.

After cloning, create the local credential file from the included template:

```bash
cp env.example env
```

Then edit `env` and provide your own key:

```text
ARK_API_KEY=your-key-here
```

Never commit or share the real `env` file. It is listed in `.gitignore`; recipients must provide their own key. Share `env.example`, not `env`.

Optional variables:

- `ARK_BASE_URL`
- `SEEDREAM_MODEL`
- `PORT`

Sprite-sheet export requires `ffmpeg` and `ffprobe` on your PATH. Image and
video generation use only Python's standard library.

## Share as a repository

```bash
git init
git add .
git status  # verify env and generated outputs are absent
git commit -m "Initial Seedream image generator"
```

The app uses only Python's standard library, so there is no install step.
