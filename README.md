# Seedream Image Generator

A tiny, dependency-free local app that sends prompts to BytePlus ModelArk/Seedream and saves every returned image under `outputs/`.

Generated images persist across page reloads and server restarts. Deleting an
image in the UI moves it to `outputs/.trash/` rather than permanently erasing
it. Deleting an attached image automatically removes it from the prompt.
Uploaded reference images are saved automatically under `inputs/`; that folder
is also created at runtime and ignored by Git.

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
└── outputs/             # created automatically at runtime; ignored by Git
```

## Configuration

The server reads `env` from the repository root—the same folder as `server.py`.
It needs either `ARK_API_KEY` or `SEEDANCE_API_KEY`.

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

## Share as a repository

```bash
git init
git add .
git status  # verify env and generated outputs are absent
git commit -m "Initial Seedream image generator"
```

The app uses only Python's standard library, so there is no install step.
