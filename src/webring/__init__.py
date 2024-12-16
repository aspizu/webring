from __future__ import annotations

import logging
import re
from http.client import (
    BAD_REQUEST,
    INTERNAL_SERVER_ERROR,
    NOT_FOUND,
    TEMPORARY_REDIRECT,
    UNAUTHORIZED,
)
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from .db import db
from .env import env
from .misc import seconds_since_epoch

if TYPE_CHECKING:
    from psycopg import AsyncCursor
    from starlette.requests import Request


URL_REGEX = re.compile(r"https?://[^/]+/?")
MIN_PASSWORD_LENGTH = 8
MAX_STATUS_LENGTH = 256

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="templates")


async def random_redirect(_request: Request) -> Response:
    logger.info("random redirect")
    async with await db() as conn, conn.cursor() as cur:
        await cur.execute("SELECT * FROM site ORDER BY RANDOM() LIMIT 1")
        account = await cur.fetchone()
    if account is None:
        return HTMLResponse(
            "<h1>There are no sites in the webring :(</h1>",
            status_code=NOT_FOUND,
        )
    return Response(status_code=TEMPORARY_REDIRECT, headers={"Location": account.url})


async def find_next(cur: AsyncCursor[Any], this_id: int) -> Any:
    await cur.execute("SELECT * FROM site WHERE id = %s", [this_id])
    this = await cur.fetchone()
    if this is None:
        return None
    await cur.execute("SELECT * FROM site WHERE id = %s", [this.next])
    next_ = await cur.fetchone()
    if next_ is None:
        return None
    if next_.valid:
        return next_
    return await find_next(cur, next_.id)


async def find_previous(cur: AsyncCursor[Any], this_id: int) -> Any:
    await cur.execute("SELECT * FROM site WHERE id = %s", [this_id])
    this = await cur.fetchone()
    if this is None:
        return None
    await cur.execute("SELECT * FROM site WHERE id = %s", [this.previous])
    previous = await cur.fetchone()
    if previous is None:
        return None
    if previous.valid:
        return previous
    return await find_previous(cur, previous.id)


async def next_redirect(request: Request) -> Response:
    this_id = int(request.query_params["site"])
    async with await db() as conn, conn.cursor() as cur:
        next_ = await find_next(cur, this_id)
        if next_ is None:
            return HTMLResponse(
                "<h1>There is no next site :(</h1>",
                status_code=NOT_FOUND,
            )
        return Response(status_code=TEMPORARY_REDIRECT, headers={"Location": next_.url})


async def previous_redirect(request: Request) -> Response:
    this_id = int(request.query_params["site"])
    async with await db() as conn, conn.cursor() as cur:
        previous = await find_previous(cur, this_id)
        if previous is None:
            return HTMLResponse(
                "<h1>There is no previous site :(</h1>",
                status_code=NOT_FOUND,
            )
        return Response(
            status_code=TEMPORARY_REDIRECT, headers={"Location": previous.url}
        )


async def register(request: Request) -> Response:
    error = None
    if request.method == "POST":
        formdata = await request.form()
        url = formdata.get("url")
        email = formdata.get("email")
        password = formdata.get("password")
        if not (
            isinstance(url, str)
            and isinstance(email, str)
            and isinstance(password, str)
            and len(password) >= MIN_PASSWORD_LENGTH
            and URL_REGEX.fullmatch(url)
            and "@" in email
            and "." in email
        ):
            return Response("invalid form data", status_code=BAD_REQUEST)
        url = url.removesuffix("/")
        async with await db() as conn, conn.cursor() as cur:
            await cur.execute("SELECT * FROM site WHERE url = %(url)s", {"url": url})
            if await cur.fetchone() is not None:
                error = "That URL is already in the webring."
            else:
                await cur.execute(
                    """
                    INSERT INTO site (url, email, password_hash, created_at)
                    VALUES (%(url)s, %(email)s, %(password_hash)s, %(created_at)s)
                    RETURNING id
                    """,
                    {
                        "url": url,
                        "email": email,
                        "password_hash": password,
                        "created_at": seconds_since_epoch(),
                    },
                )
                site = await cur.fetchone()
                if site is None:
                    logger.error("site is none")
                    return Response(status_code=INTERNAL_SERVER_ERROR)
                await cur.execute(
                    "UPDATE site SET next = %(this_id)s WHERE next = null RETURNING id",
                    {"this_id": site.id},
                )
                previous_site = await cur.fetchone()
                if previous_site is not None:
                    await cur.execute(
                        """
                        UPDATE site SET previous = %(previous_id)s
                        WHERE id = %(this_id)s
                        """,
                        {"previous_id": previous_site.id, "this_id": site.id},
                    )
    return templates.TemplateResponse(
        "register.jinja", {"request": request, "error": error}
    )


async def deregister(request: Request) -> Response:
    error = None
    success = False
    if request.method == "POST":
        formdata = await request.form()
        url = formdata.get("url")
        password = formdata.get("password")
        if not (
            isinstance(url, str)
            and isinstance(password, str)
            and len(password) >= MIN_PASSWORD_LENGTH
            and URL_REGEX.fullmatch(url)
        ):
            return Response("invalid form data", status_code=BAD_REQUEST)
        url = url.removesuffix("/")
        async with await db() as conn, conn.cursor() as cur:
            await cur.execute("SELECT * FROM site WHERE url = %(url)s", {"url": url})
            site = await cur.fetchone()
            if site is None:
                error = "That URL is not in the webring."
            elif password != site.password_hash:
                error = "Incorrect password."
            else:
                await cur.execute(
                    "UPDATE site SET next = %(next)s WHERE id = %(previous)s",
                    {
                        "next": site.next,
                        "previous": site.previous,
                    },
                )
                await cur.execute(
                    "UPDATE site SET previous = %(previous)s WHERE id = %(next)s",
                    {
                        "next": site.next,
                        "previous": site.previous,
                    },
                )
                await cur.execute(
                    "DELETE FROM site WHERE id = %(site_id)s",
                    {"site_id": site.id},
                )
                success = True
    return templates.TemplateResponse(
        "deregister.jinja", {"request": request, "error": error, "success": success}
    )


async def widget(request: Request) -> Response:
    error = None
    widget = None
    if request.method == "POST":
        formdata = await request.form()
        url = formdata.get("url")
        if not (isinstance(url, str) and URL_REGEX.fullmatch(url)):
            return Response("invalid form data", status_code=BAD_REQUEST)
        url = url.removesuffix("/")
        async with await db() as conn, conn.cursor() as cur:
            await cur.execute("SELECT * FROM site WHERE url = %(url)s", {"url": url})
            site = await cur.fetchone()
            if site is None:
                error = "That URL is not in the webring."
            else:
                widget = (
                    f'<a href="{env.HOST}/previous?site={site.id}">'
                    f'<img src="{env.HOST}/static/previous.png" alt="Previous Site">'
                    f"</a>"
                    f'<a href="{env.HOST}/?site={site.id}">'
                    f'<img src="{env.HOST}/static/logo.png" alt="WebRing">'
                    f"</a>"
                    f'<a href="{env.HOST}/next?site={site.id}">'
                    f'<img src="{env.HOST}/static/next.png" alt="Next Site">'
                    f"</a>"
                    f'<a href="{env.HOST}/random?site={site.id}">'
                    f'<img src="{env.HOST}/static/random.png" alt="Random Site">'
                    f"</a>"
                )
    return templates.TemplateResponse(
        "widget.jinja",
        {
            "request": request,
            "error": error,
            "widget": widget,
        },
    )


async def ring(request: Request) -> Response:
    limit = int(request.query_params.get("limit", 10))
    offset = int(request.query_params.get("offset", 0))
    async with await db() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT * FROM site
            WHERE valid = true
            ORDER BY id
            LIMIT %(limit)s
            OFFSET %(offset)s
            """,
            {
                "limit": limit,
                "offset": offset,
            },
        )
        sites = await cur.fetchall()
    return templates.TemplateResponse(
        "ring.jinja",
        {
            "request": request,
            "sites": sites,
        },
    )


async def login(request: Request) -> Response:
    error = None
    success = False
    if request.method == "POST":
        formdata = await request.form()
        email = formdata.get("email")
        password = formdata.get("password")
        if not (
            isinstance(email, str)
            and isinstance(password, str)
            and "@" in email
            and "." in email
        ):
            return Response("invalid form data", status_code=BAD_REQUEST)
        async with await db() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM site WHERE email = %(email)s", {"email": email}
            )
            site = await cur.fetchone()
            if site is None:
                error = "That email is not in the webring."
            elif password != site.password_hash:
                error = "Incorrect password."
            else:
                request.session["site"] = site.id
                request.session["email"] = email
                success = True
    return templates.TemplateResponse(
        "login.jinja", {"request": request, "error": error, "success": success}
    )


async def index(request: Request) -> Response:
    status = None
    statuses = []
    if request.session["site"]:
        async with await db() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT status FROM status WHERE site = %(site)s
                ORDER BY created_at DESC LIMIT 1
                """,
                {"site": request.session["site"]},
            )
            status = await cur.fetchone()
            status = None if status is None else status.status
            await cur.execute(
                """
                SELECT * FROM status WHERE site = %(site)s
                ORDER BY created_at DESC
                """,
                {"site": request.session["site"]},
            )
            statuses = await cur.fetchall()
    if request.method == "POST":
        formdata = await request.form()
        status = formdata.get("status")
        if not (isinstance(status, str) and len(status) <= MAX_STATUS_LENGTH):
            return Response("invalid form data", status_code=BAD_REQUEST)
        if request.session["site"] is None:
            return Response("not logged in", status_code=UNAUTHORIZED)
        async with await db() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO status (site, status, created_at)
                VALUES (%(site)s, %(status)s, %(created_at)s) RETURNING id
                """,
                {
                    "site": request.session["site"],
                    "status": status,
                    "created_at": seconds_since_epoch(),
                },
            )

    return templates.TemplateResponse(
        "index.jinja",
        {
            "request": request,
            "status": status,
            "host": env.HOST,
            "statuses": statuses,
        },
    )


async def get_status(request: Request) -> Response:
    site = request.query_params.get("site")
    if not (isinstance(site, str) and site.isdigit()):
        return JSONResponse(
            {"success": False, "message": "site? NOT PROVIDED OR NOT INTEGER"}
        )
    async with await db() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT * FROM status WHERE site = %(site)s
            ORDER BY created_at DESC LIMIT 1
            """,
            {"site": site},
        )
        status = await cur.fetchone()
        if status is None:
            return JSONResponse({"success": False, "message": "NO STATUS FOUND"})
        return JSONResponse(
            {
                "success": True,
                "status": status.status,
                "id": status.id,
                "created_at": status.created_at,
            }
        )


app = Starlette(
    debug=env.DEBUG,
    routes=[
        Route("/get_status", get_status),
        Route("/register", register, methods=["GET", "POST"]),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/deregister", deregister, methods=["GET", "POST"]),
        Route("/", index, methods=["GET", "POST"]),
        Route("/widget", widget, methods=["GET", "POST"]),
        Route("/ring", ring),
        Route("/random", random_redirect),
        Route("/previous", previous_redirect),
        Route("/next", next_redirect),
        Mount("/static", StaticFiles(directory="static"), name="static"),
    ],
    middleware=[
        Middleware(
            SessionMiddleware,
            secret_key=env.SECRET_KEY,
            https_only=True,
            same_site="strict",
        )
    ],
)
