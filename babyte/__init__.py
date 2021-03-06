import os
import sqlite3
from functools import wraps
from xml.etree import ElementTree as ET

import httplib2
from flask import (
    Flask, abort, flash, g, redirect, render_template, request, session,
    url_for)
from oauth2client.client import OAuth2WebServerFlow


app = Flask(__name__)
app.config.update(dict(
    DATABASE='/tmp/babyte.db',
    SECRET_KEY='secret key',
    TESTING=os.environ.get('TESTING'),
    OAUTH_CLIENT_ID='oauth client id',
    OAUTH_SECRET_KEY='oauth secret key',
    OAUTH_REDIRECT='http://localhost:5000/oauth2callback',
    OAUTH_SCOPE='https://www.googleapis.com/auth/contacts.readonly',
    DOMAIN='kozea.fr'
))
app.config.from_envvar('BABYTE_SETTINGS', silent=True)


if app.debug:
    from sassutils.wsgi import SassMiddleware
    app.wsgi_app = SassMiddleware(app.wsgi_app, {
        'babyte': ('static', 'static', '/static')})


class User:
    def __init__(self, name):
        self.name = name
        self.ranking = 1000
        self.number_of_match = 0


FLOW = OAuth2WebServerFlow(
    client_id=app.config['OAUTH_CLIENT_ID'],
    client_secret=app.config['OAUTH_SECRET_KEY'],
    redirect_uri=app.config['OAUTH_REDIRECT'],
    scope=app.config['OAUTH_SCOPE'],
    user_agent='babyte/1.0')


def connect_db():
    """Connects to the specific databse. """
    rv = sqlite3.connect(app.config['DATABASE'])
    rv.row_factory = sqlite3.Row
    return rv


def get_db():
    """Opens a new database connection
    if there is none yet for the current application context."""
    if not hasattr(g, 'sqlite_db'):
        g.sqlite_db = connect_db()
        return g.sqlite_db


def auth(function):
    """Wrapper checking if the user is logged in."""
    @wraps(function)
    def wrapper(*args, **kwargs):
        testing = app.config.get('TESTING')
        if (session.get('users') and session.get('logged_in')) or testing:
            return function(*args, **kwargs)
        return redirect(FLOW.step1_get_authorize_url())
    return wrapper


@app.route('/oauth2callback')
def oauth2callback():
    code = request.args.get('code')
    credentials = FLOW.step2_exchange(code)
    http = credentials.authorize(httplib2.Http())
    _, content = http.request(
        'https://www.google.com/m8/feeds/gal/'
        '{}/full'.format(app.config['DOMAIN']))
    root = ET.fromstring(content)
    session['users'] = []
    for entry in root:
        if entry.tag == '{http://www.w3.org/2005/Atom}entry':
            for item in entry:
                if item.tag == '{http://schemas.google.com/gal/2009}type':
                    if item.attrib['type'] != 'profile':
                        break
                if item.tag == '{http://schemas.google.com/g/2005}name':
                    for name in item:
                        if name.tag == (
                                '{http://schemas.google.com/g/2005}fullName'):
                            session['users'].append(name.text)
    if session['users']:
        session['logged_in'] = True
        return redirect(url_for('home'))
    else:
        return abort('404')


@app.teardown_appcontext
def close_db(error):
    """Closes the database again at the end of the request."""
    if hasattr(g, 'sqlite_db'):
        g.sqlite_db.close()


@app.cli.command('init_db')
def init_db():
    db = get_db()
    with app.open_resource('babyte.sql', mode='r') as f:
        db.cursor().executescript(f.read())
    db.commit()


@app.route('/')
@auth
def home():
    ranking = compute_ranking()
    return render_template('home.html', ranking=ranking)


@app.route('/add', methods=['POST'])
@auth
def add_match():
    if not request.form['team1_player1'] or not request.form['team2_player1']:
        return redirect(url_for('home'))
    db = get_db()
    db.execute(
        'insert into match (team1_player1, team1_player2, team2_player1, '
        'team2_player2, score_team1, score_team2) values (?, ?, ? ,?, ?, ?)',
        [request.form['team1_player1'], request.form['team1_player2'],
         request.form['team2_player1'], request.form['team2_player2'],
         request.form['score_team1'], request.form['score_team2']])
    db.commit()
    flash('Match ajouté avec succès !')
    return redirect(url_for('home'))


@app.route('/list')
@app.route('/list/<user>')
@auth
def list(user=None):
    db = get_db()
    if user:
        cur = db.execute(
            'select id, team1_player1, team1_player2, team2_player1, '
            'team2_player2, score_team1, score_team2 from match '
            'where team1_player1=? or team1_player2=? or '
            'team2_player1=? or team2_player2=? order by id desc',
            [user] * 4)
    else:
        cur = db.execute(
            'select id, team1_player1, team1_player2, team2_player1, '
            'team2_player2, score_team1, score_team2 from match '
            'order by id desc')
    matches = cur.fetchall()
    return render_template('list.html', matches=matches)


def compute_ranking():
    """Get the list of all users with their current score."""
    users = {}
    db = get_db()
    cur = db.execute(
        'select id, team1_player1, team1_player2, team2_player1, '
        'team2_player2, score_team1, score_team2 from match order by id asc')
    matches = cur.fetchall()
    for match in matches:
        if match['team1_player1'] not in users:
            users[match['team1_player1']] = User(match['team1_player1'])
        team1_player1 = users[match['team1_player1']]
        if match['team2_player1'] not in users:
            users[match['team2_player1']] = User(match['team2_player1'])
        team2_player1 = users[match['team2_player1']]
        team1_player2 = team2_player2 = None
        if match['team1_player2']:
            if match['team1_player2'] not in users:
                users[match['team1_player2']] = User(match['team1_player2'])
            team1_player2 = users[match['team1_player2']]
        if match['team2_player2']:
            if match['team2_player2'] not in users:
                users[match['team2_player2']] = User(match['team2_player2'])
            team2_player2 = users[match['team2_player2']]
        elo(team1_player1, team1_player2, team2_player1, team2_player2,
            match['score_team1'], match['score_team2'])
    return users


def elo(team1_player1, team1_player2, team2_player1, team2_player2,
        score_team1, score_team2):
    """Update the ranking of each players in parameters.
    Calculate the score of the match according to the following formula:
    Rn = Ro + KG (W - We)
    See: https://fr.wikipedia.org /wiki/Classement_mondial_de_football_Elo
    """
    elo1, number_match1 = fictive_player(team1_player1, team1_player2)
    elo2, number_match2 = fictive_player(team2_player1, team2_player2)

    score_p1 = compute_fictive_score(score_team1, score_team2,
                                     elo1, elo2, number_match1)
    score_p2 = compute_fictive_score(score_team2, score_team1,
                                     elo2, elo1, number_match2)

    update_score(team1_player1, team1_player2, score_p1)
    update_score(team2_player1, team2_player2, score_p2)


def fictive_player(player_1, player_2):
    """Create fictive player for elo computation."""
    if player_2 is None:
        elo = player_1.ranking
        number_match = player_1.number_of_match
    else:
        elo = (player_1.ranking + player_2.ranking) / 2
        number_match = (
            player_1.number_of_match +
            player_2.number_of_match) / 2
    return elo, number_match


def compute_fictive_score(score_team, score_opponent, elo_team, elo_opponent,
                          nb_match_team):
    """Compute fictive score for elo ranking."""
    expected_result = 1 / (1 + 10 ** ((elo_opponent - elo_team) / 400))
    expertise = get_expertise_coefficient(nb_match_team, elo_team)
    goal_difference = get_goal_difference_coefficient(
        score_team, score_opponent)
    result = 1 if score_team > score_opponent else 0
    score = expertise * goal_difference * (result - expected_result)
    return score


def get_expertise_coefficient(number_of_match, elo):
    """Get expertise coefficient corresponding to an user's elo and matches."""
    if number_of_match < 40:
        return 40
    elif elo < 2400:
        return 20
    else:
        return 10


def get_goal_difference_coefficient(score_team_1, score_team_2):
    """Get goal difference coefficient corresponding to a match score."""
    diff = abs(score_team_1 - score_team_2)
    if diff < 2:
        return 1
    elif diff == 2:
        return 1.5
    elif diff == 3:
        return 1.75
    else:
        return 1.75 + (diff - 3) / 8


def update_score(player_1, player_2, score):
    """Update score during elo computation."""
    player_1.number_of_match += 1
    if player_2 is None:
        player_1.ranking += round(score)
    else:
        sum_ranking = player_1.ranking + player_2.ranking
        player_1.ranking += round(
            player_1.ranking / sum_ranking * score)
        player_2.ranking += round(
            player_2.ranking / sum_ranking * score)
        player_2.number_of_match += 1
