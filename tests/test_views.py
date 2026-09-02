"""The server-rendered Jinja2 views.

These are the pages at `/` and `/leaderboard`, separate from the React SPA at
`/app`. Only the parts with a decision behind them are pinned here.
"""


def test_the_leaderboard_view_defaults_to_the_first_configured_mode(client):
    """A visitor with no `?game_mode=` must land on a mode that exists.

    The default was the literal "classic" while every link in the templates
    pointed at "blitz", so which page a visitor actually got depended on which
    of the two names the database happened to carry -- and a database with
    neither rendered an empty board titled after a mode it did not have.

    Asserting the identity rather than a mode name: requesting no mode must
    render exactly what requesting the first configured one renders.
    """

    modes = client.get("/api/leaderboard/game_modes").json()
    assert modes, "the default is only meaningful when modes are configured"
    first = modes[0]["name"]

    default_page = client.get("/leaderboard")
    explicit = client.get(f"/leaderboard?game_mode={first}")

    assert default_page.status_code == 200
    assert explicit.status_code == 200
    assert default_page.text == explicit.text


def test_the_landing_mode_is_the_alphabetically_first_one(client):
    """The rule the README states, and the reason it is worth stating.

    Neither client names a default any more; both take the first row of
    /game_modes, which is ordered by `name`. So the landing page is decided by
    alphabetical order, and adding a mode whose name sorts earlier moves it.
    That is an acceptable rule but not an obvious one -- if `ORDER BY name`
    changes, the documentation is wrong and this fails.
    """

    modes = [mode["name"] for mode in client.get("/api/leaderboard/game_modes").json()]

    assert modes, "the rule is only meaningful with modes configured"
    assert modes == sorted(modes), "/game_modes is documented as ordered by name"
    assert modes[0] == min(modes)


def test_the_templates_name_no_game_mode(client):
    """The nav must not hardcode a mode the deployment may not have.

    `/leaderboard?game_mode=blitz` appeared in both templates under the label
    "Classic", naming a mode absent from the seed. Linking to the bare route
    lets the view pick a mode that is actually configured.
    """

    for path in ("/", "/leaderboard"):
        page = client.get(path)
        assert page.status_code == 200
        assert "game_mode=blitz" not in page.text
