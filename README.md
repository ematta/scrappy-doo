# scrappy-doo

Run unit tests with Python 3.13 or newer:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The OpenRouter adapter reads `OPENROUTER_API_KEY` from the environment unless
an API key is supplied explicitly. It defaults to `z-ai/glm-5.3-flash`; the
model can be changed when creating the adapter. Browser and CLI integration are
still in development.
