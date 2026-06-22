To run:

Put extracted data inside `vendor/assets`. For example, `vendor/assets/db` should contain a bunch of json files.

Make sure `uv` is installed. Run
```shell
uv run main.py
```

For subdirectories such as `scripts`, run
```shell
uv run -m scripts.upload_icons
```
