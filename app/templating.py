from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.security.csrf import csrf_input

templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")
templates.env.globals["csrf_input"] = csrf_input  # use {{ csrf_input(request) }} inside every <form method="post">
