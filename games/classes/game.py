from abc import ABC, abstractmethod
import secrets
import json
import random
import time
import math
from ..models import GameType, Participation
from .cards_utils import get_cards_deck, get_random_hand
from ..redis_utils import redis
from ..ranking import calculate_elo

HASH_GAME_LEN = 4
WAITING = 'waiting'
ONGOING = 'ongoing'
FINISHED = 'finished'
MAX_TIMEOUT = 30
INACTIVE_PINGS_DISC = 5


class Game(ABC):
    @classmethod
    def get_config_json(cls):
        with open(f'games/games_configs/{cls.__name__}.json', 'r') as jsonFile:
            jsonObject = json.load(jsonFile)
            jsonFile.close()
        return jsonObject

    @classmethod
    def path_to_game(cls, game_id):
        return cls.__name__.lower() + '.' + game_id

    @classmethod
    def create_game(cls, user_json):
        type_game = cls.__name__.lower()
        # Redis identifier can only begin with a letter, a dollar sign or an underscore
        id = 'g' + secrets.token_hex(HASH_GAME_LEN)
        while redis.jsontype('games', f'.{type_game}.{id}'):
            id = 'g' + secrets.token_hex(HASH_GAME_LEN)

        user_json['players'] = {}
        user_json['status'] = 'waiting'
        # user_json['id'] = id

        # Create redis instance games: {type_game: {}} if does not exist
        redis.jsonset('games', '.', {}, nx=True)
        redis.jsonset('games', f'.{type_game}', {}, nx=True)

        # Add game to games
        redis.jsonset('games', f'.{type_game}.{id}', user_json)
        redis.jsonset('games', f'.{type_game}.{id}.any_update_in_game', False)
        redis.jsonset('games', f'.{type_game}.{id}.scores_to_users', False)
        redis.jsonset('games', f'.{type_game}.{id}.state_to_send', False)

        redis.jsonset('games', f'.{type_game}.{id}.scores', {
                      'win': [], 'lose': []})
        return id

    @classmethod
    def check_create_game(cls, user_json) -> bool:
        config_json = cls.get_config_json()
        for k in config_json.keys():
            if k != 'game_params':
                cls.check_param(
                    user_json['game_parameters'][k], config_json[k])

        for elem in config_json['game_params']:
            k = elem['param_name']
            cls.check_param(user_json['game_parameters']
                            [k], elem['param_setup'])
        return True

    def check_param(user_value, config_param):
        param_type = config_param['type']
        if param_type == 'int':
            user_value = int(user_value)
            if user_value >= config_param['min'] and user_value <= config_param['max']:
                return True
        elif param_type == 'bool':
            user_value = bool(user_value)
            return True
        elif param_type == 'time':
            def get_seconds(time):
                multipier = 1
                seconds = 0
                for el in time.split(':')[::-1]:
                    seconds += int(el) * multipier
                    multipier *= 60
                return seconds
            if get_seconds(config_param['min']) <= user_value <= get_seconds(config_param['max']):
                return True
        else:
            raise Exception('Parameter type has no implemented checking')
        raise Exception(f'{param_type} parameter is incorrect')

    @classmethod
    def delete_game(cls, game_id):
        game = cls.path_to_game(game_id)
        redis.jsondel('games', f'.{game}')

    @classmethod
    def get_first_possible_chair(cls, game_id):
        game = cls.path_to_game(game_id)
        chairs = redis.jsonget('games', f'.{game}.players').keys()
        for i in range(redis.jsonget('games', f'.{game}.game_parameters.max_players')):
            if 'p' + str(i+1) not in chairs:
                return 'p' + str(i+1)

    @classmethod
    def get_user_chair(cls, game_id, user):
        game = cls.path_to_game(game_id)
        for chair, values in redis.jsonget('games', f'.{game}.players').items():
            if values['nickname'] == user:
                return chair
        return None

    @classmethod
    def get_user_chair_by_nicknameshow(cls, game_id, nicknameshow):
        game = cls.path_to_game(game_id)
        for chair, values in redis.jsonget('games', f'.{game}.players').items():
            if values['nickname_show'] == nicknameshow:
                return chair
        return None

    @classmethod
    def connect_to(cls, game_id, user):
        game = cls.path_to_game(game_id)
        # user = {nickname, ranking}
        user['ready'] = False
        user['active'] = True
        user['nickname_show'] = user['nickname']
        max_players = redis.jsonget(
            'games', f'.{game}.game_parameters.max_players')
        players = redis.jsonget('games', f'.{game}.players')
        if user['nickname'] in cls.get_all_players(game_id):
            chair = cls.get_user_chair(game_id, user['nickname'])
            redis.jsonset('games', f'.{game}.players.{chair}.active', True)
            redis.jsonset('games',
                          f'.{game}.players.{chair}.inactive_pings', 0)
            return True
        elif redis.jsonget('games', f'.{game}.status') == WAITING and max_players > len(players):
            if cls.get_user_chair(game_id, user):
                return True
            # if cls.is_user_in_any_game(user['nickname']):
            #     return False
            chair = cls.get_first_possible_chair(game_id)
            redis.jsonset('games', f'.{game}.players.{chair}', user)
            redis.jsonset('games',
                          f'.{game}.players.{chair}.inactive_pings', 0)
            return True
        return False

    @classmethod
    def disconnect_from(cls, game_id, user):
        game = cls.path_to_game(game_id)
        chair = cls.get_user_chair(game_id, user)
        status = redis.jsonget('games', f'.{game}.status')
        if status == WAITING or status == FINISHED:
            redis.jsondel('games', f'.{game}.players.{chair}')
            if len(redis.jsonget('games', f'.{game}.players')) == 0:
                print(redis.jsonget('games', f'.{game}'))
        elif status == ONGOING:
            redis.jsonset('games', f'.{game}.players.{chair}.active', False)
            cls.start_counting_timeout(game_id, chair)
        redis.jsonset('games', f'.{game}.any_update_in_game', True)

    @classmethod
    def mark_ready(cls, game_id, user, value: bool):
        game = cls.path_to_game(game_id)
        chair = cls.get_user_chair(game_id, user)
        if isinstance(value, bool):
            redis.jsonset('games', f'.{game}.players.{chair}.ready', value)

    @classmethod
    def mark_active(cls, game_id, user, value: bool):
        game = cls.path_to_game(game_id)
        chair = cls.get_user_chair(game_id, user)
        if isinstance(value, bool):
            redis.jsonset('games', f'.{game}.players.{chair}.active', value)
            if value:
                redis.jsonset(
                    'games', f'.{game}.players.{chair}.inactive_pings', 0)
            else:
                
                print(f'add inactive_ping to {user}')
                cls.add_inactive_ping(game_id, chair)
                if redis.jsonget('games', f'.{game}.players.{chair}.inactive_pings') == INACTIVE_PINGS_DISC:
                    cls.disconnect_from(game_id, user)
                elif redis.jsonget('games', f'.{game}.players.{chair}.inactive_pings') > INACTIVE_PINGS_DISC:                        
                    redis.jsonset('games', f'.{game}.any_update_in_game', True)
                    if not cls.is_game_ongoing(game_id):
                        cls.disconnect_from(game_id, user)
        

    @classmethod
    def add_inactive_ping(cls, game_id, chair):
        game = cls.path_to_game(game_id)
        redis.jsonnumincrby(
            'games', f'.{game}.players.{chair}.inactive_pings', 1)

    @classmethod
    def game_info(cls, game_id):
        game = cls.path_to_game(game_id)
        info = {}
        info['players'] = []
        players = redis.jsonget('games', f'.{game}.players')

        max_players = redis.jsonget(
            'games', f'.{game}.game_parameters.max_players')
        info['max_players'] = max_players
        for p, values in players.items():
            player = {}
            player['position'] = p
            player['nickname'] = values['nickname_show']
            player['ranking'] = values['ranking']
            player['ready'] = values['ready']
            player['active'] = values['inactive_pings'] <= 2
            info['players'].append(player)
        for i in range(len(info), max_players):
            info['players']['p' + str(i+1)] = None
        info['status'] = redis.jsonget('games', f'.{game}.status')
        print(info)
        return info

    @classmethod
    def get_all_players(cls, game_id):
        game = cls.path_to_game(game_id)
        nicknames = []
        for p, values in redis.jsonget('games', f'.{game}.players').items():
            nicknames.append(values['nickname'])
        return nicknames

    @classmethod
    def get_all_user_ids(cls, game_id):
        game = cls.path_to_game(game_id)
        ids = []
        for p, values in redis.jsonget('games', f'.{game}.players').items():
            ids.append(values['id'])
        return ids

    @classmethod
    def get_all_chairs(cls, game_id):
        game = cls.path_to_game(game_id)
        return redis.jsonget('games', f'.{game}.players').keys()

    @classmethod
    def get_players_ids(cls, game_id):
        game = cls.path_to_game(game_id)
        return [val['id'] for _, val in redis.jsonget('games', f'.{game}.players').items()]

    @classmethod
    def get_id_from_nickname(cls, game_id, nickname):
        game = cls.path_to_game(game_id)
        for p, values in redis.jsonget('games', f'.{game}.players').items():
            if values['nickname'] == nickname:
                return values['id']

    @classmethod
    def get_nickname_from_id(cls, game_id, id):
        game = cls.path_to_game(game_id)
        for p, values in redis.jsonget('games', f'.{game}.players').items():
            if values['id'] == id:
                return values['nickname']

    @classmethod
    def get_hand(cls, game_id, user):
        game = cls.path_to_game(game_id)
        chair = cls.get_user_chair(game_id, user)
        return redis.jsonget('games', f'.{game}.players.{chair}.hand')

    @classmethod
    def current_username(cls, game_id):
        game = cls.path_to_game(game_id)
        current_player = cls.current_player(game_id)
        for p, values in redis.jsonget('games', f'.{game}.players').items():
            if p == current_player:
                return values['nickname']

    @classmethod
    def current_player(cls, game_id):
        game = cls.path_to_game(game_id)
        if redis.jsonget('games', f'.{game}.status') == ONGOING:
            return redis.jsonget('games', f'.{game}.current_player')

    @classmethod
    def start_game_possible(cls, game_id):
        game = cls.path_to_game(game_id)
        if redis.jsonget('games', f'.{game}.status') != WAITING \
                and redis.jsonget('games', f'.{game}.status') != ONGOING:
            return False
        max_players = redis.jsonget(
            'games', f'.{game}.game_parameters.max_players')
        players = len(redis.jsonget('games', f'.{game}.players'))

        if max_players == players:
            for values in redis.jsonget('games', f'.{game}.players').values():
                if values['ready'] == False:
                    return False
        else:
            return False
        return True

    @classmethod
    def start_game(cls, game_id):
        game = cls.path_to_game(game_id)
        redis.jsonset('games', f'.{game}.status', ONGOING)
        card_deck = get_cards_deck()
        for player in redis.jsonget('games', f'.{game}.players'):
            card_deck, cards = get_random_hand(card_deck, redis.jsonget(
                'games', f'.{game}.game_parameters.cards_on_hand'))
            redis.jsonset('games', f'.{game}.players.{player}.hand', cards)
            u_time = redis.jsonget('games',
                                   f'.{game}.game_parameters.time_per_player')
            redis.jsonset('games', f'.{game}.players.{player}.time', u_time)
            redis.jsonset('games', f'.{game}.players.{player}.points', 0)
            redis.jsonset('games', f'.{game}.players.{player}.timeout',
                          MAX_TIMEOUT)

        starting_player = random.choice(
            list(redis.jsonget('games', f'.{game}.players').keys()))
        redis.jsonset('games', f'.{game}.starting_player', starting_player)
        redis.jsonset('games', f'.{game}.current_player', starting_player)

        redis.jsonset('games', f'.{game}.stack_draw', card_deck)
        redis.jsonset('games', f'.{game}.stack_throw', [])
        redis.jsonset('games', f'.{game}.move_time', time.time())
        redis.jsonset('games', f'.{game}.end_by_timeout', False)
        redis.jsonset('games', f'.{game}.surrender', False)
        redis.jsonset('games', f'.{game}.is_draw', False)
        redis.jsonset('games', f'.{game}.scores_to_rabbit', False)
        redis.jsonset('games', f'.{game}.scores_to_users', False)
        redis.jsonset('games', f'.{game}.state_to_send', False)
        redis.jsonset('games', f'.{game}.scores', {
                      'win': [], 'lose': []})

    @classmethod
    def game_state(cls, game_id):
        game = cls.path_to_game(game_id)
        players = []
        for player, values in redis.jsonget('games', f'.{game}.players').items():
            player_info = {}
            player_info['cards_hand'] = redis.jsonarrlen('games',
                                                         f'.{game}.players.{player}.hand')
            player_info['time'] = math.ceil(redis.jsonget('games',
                                                          f'.{game}.players.{player}.time'))
            player_info['points'] = redis.jsonget('games',
                                                  f'.{game}.players.{player}.points')
            player_info['position'] = player
            players.append(player_info)

        stack_draw = redis.jsonarrlen('games', f'.{game}.stack_draw')
        stack_throw = redis.jsonarrlen('games', f'.{game}.stack_throw')
        cards_top = redis.jsonget('games', f'.{game}.stack_throw')
        if cards_top:
            cards_top = cards_top[-1]
        else:
            cards_top = '--'
        current_user = cls.current_player(game_id)

        state = {
            'current_user': current_user,
            'players': players,
            'stack_draw': stack_draw,
            'stack_throw': stack_throw,
            'cards_top': cards_top,
        }
        return state

    @classmethod
    def debug_info(cls, game_id):
        game = cls.path_to_game(game_id)
        info = redis.jsonget('games', f'.{game}')
        print(info)
        return info

    @classmethod
    def get_next_player(cls, game_id):
        game = cls.path_to_game(game_id)
        players = list(redis.jsonget('games', f'.{game}.players').keys())
        curr_player = redis.jsonget('games', f'.{game}.current_player')
        return_player = players[0]
        for p in players[::-1]:
            if curr_player == p:
                return return_player
            return_player = p

    @classmethod
    def is_user_in_any_game(cls, user):
        for game_id in redis.jsonget('games', f'.{cls.__name__.lower()}'):
            if cls.get_user_chair(game_id, user):
                return True
        return False

    @classmethod
    def is_game_ongoing(cls, game_id):
        game = cls.path_to_game(game_id)
        return redis.jsonget('games', f'.{game}.status') == ONGOING

    @classmethod
    def surrender(cls, game_id, user):
        game = cls.path_to_game(game_id)
        redis.jsonset('games', f'.{game}.surrender', True)
        cls.finish_game(game_id, [user])

    @classmethod
    def finish_game(cls, game_id, lose_users):
        if cls.is_game_ongoing(game_id):
            game = cls.path_to_game(game_id)
            players = cls.get_all_players(game_id)
            if not redis.jsonget('games', f'.{game}.is_draw'):
                lose_nicknames = []
                for loser in lose_users:
                    lose_nicknames.append(
                        cls.get_nicknameshow_by_nickname(game_id, loser))
                    players.remove(loser)

                redis.jsonset('games', f'.{game}.scores.lose', lose_nicknames)
                for p in players:
                    win_nickname = cls.get_nicknameshow_by_nickname(game_id, p)
                    redis.jsonarrappend('games', f'.{game}.scores.win', win_nickname)
            redis.jsonset('games', f'.{game}.status', FINISHED)
            redis.jsonset('games', f'.{game}.any_update_in_game', True)
            redis.jsonset('games', f'.{game}.scores_to_users', True)
            redis.jsonset('games', f'.{game}.state_to_send', True)

            cls.update_db_after_finish(game_id)

    @classmethod
    def get_nickname_by_nicknameshow(cls, game_id, nickname_show):
        game = cls.path_to_game(game_id)
        for p, values in redis.jsonget('games', f'.{game}.players').items():
            if values['nickname_show'] == nickname_show:
                return values['nickname']

    @classmethod
    def get_nicknameshow_by_nickname(cls, game_id, nickname):
        game = cls.path_to_game(game_id)
        for p, values in redis.jsonget('games', f'.{game}.players').items():
            if values['nickname'] == nickname:
                return values['nickname_show']

    @classmethod
    def update_db_after_finish(cls, game_id):
        print('UPDATING DB')
        game = cls.path_to_game(game_id)
        draw = Participation.ScoreTypes.DRAW
        if redis.jsonget('games', f'.{game}.end_by_timeout'):
            win = Participation.ScoreTypes.WIN_BY_DISCONNECT
            lose = Participation.ScoreTypes.LOSE_BY_DISCONNECT
        else:
            win = Participation.ScoreTypes.WIN
            lose = Participation.ScoreTypes.LOSE

        modeltype = GameType.objects.get_typegame_lower_nospecial(
            cls.__name__.lower())

        for p in cls.get_all_players(game_id):
            user_id = cls.get_id_from_nickname(game_id, p)
            nick = cls.get_nicknameshow_by_nickname(game_id, p)
            if redis.jsonget('games', f'.{game}.is_draw'):
                score = draw
            elif nick in redis.jsonget('games', f'.{game}.scores.win'):
                score = win
            elif nick in redis.jsonget('games', f'.{game}.scores.lose'):
                score = lose

            Participation.objects.get_by_userid_gametype(
                user_id, modeltype).update(score=score)

    @classmethod
    def draw_game(cls, game_id):
        game = cls.path_to_game(game_id)
        redis.jsonset('games', f'.{game}.is_draw', True)
        redis.jsonset('games', f'.{game}.status', FINISHED)
        cls.update_db_after_finish(game_id)

    @classmethod
    def get_finish_scores(cls, game_id):
        game = cls.path_to_game(game_id)
        scores = redis.jsonget('games', f'.{game}.scores')
        if redis.jsonget('games', f'.{game}.end_by_timeout'):
            reason = 'timeout'
        elif redis.jsonget('games', f'.{game}.is_draw'):
            reason = 'draw'
        elif redis.jsonget('games', f'.{game}.surrender'):
            reason = 'surrender'
        else:
            reason = 'finish'
        return {'scores': scores, 'reason': reason}

    @classmethod
    def get_score_from_scoretype(cls, scoretype):
        if scoretype == 'lose':
            return 0
        elif scoretype == 'win':
            return 1
        elif scoretype == 'draw':
            return 0.5

    @classmethod
    def is_ranking_game(cls, game_id):
        game = cls.path_to_game(game_id)
        return redis.jsonget('games', f'.{game}.game_parameters.is_ranked')

    @classmethod
    def get_user_score(cls, game_id, nickname, scoretype):
        game = cls.path_to_game(game_id)
        info = {}
        max_time = redis.jsonget(
            'games', f'.{game}.game_parameters.time_per_player')
        chair = cls.get_user_chair(game_id, nickname)
        # points = ranking
        user_ranking = redis.jsonget('games', f'.{game}.players.{chair}.ranking')
        rankings = cls.get_all_rankings(game_id)
        idx = rankings.index(user_ranking)
        rankings.pop(idx)
        score = cls.get_score_from_scoretype(scoretype)
        k = 100
        info['points'] = int(calculate_elo(user_ranking, rankings, score, k)) - user_ranking
        info['score'] = scoretype
        info['left'] = False
        info['moves'] = 0
        info['time_sec'] = int(max_time -
                               redis.jsonget('games', f'.{game}.players.{chair}.time'))
        if nickname == cls.get_timeouted_user(game_id):
            info['left'] = True
        return info

    @classmethod
    def get_all_rankings(cls, game_id):
        game = cls.path_to_game(game_id)
        rankings = []
        for chair in redis.jsonget('games', f'.{game}.players').keys():
            rankings.append(redis.jsonget('games', f'.{game}.players.{chair}.ranking'))
        return rankings

    @classmethod
    def was_scores_sent(cls, game_id):
        game = cls.path_to_game(game_id)
        ret = redis.jsonget('games', f'.{game}.scores_to_rabbit')
        return ret

    @classmethod
    def is_state_to_send(cls, game_id):
        game = cls.path_to_game(game_id)
        state_to_send = redis.jsonget('games', f'.{game}.state_to_send')
        if state_to_send:
            redis.jsonset('games', f'.{game}.state_to_send', False)
        return state_to_send

    @classmethod
    def set_scores_send(cls, game_id, val=True):
        game = cls.path_to_game(game_id)
        redis.jsonset('games', f'.{game}.scores_to_rabbit', val)
        
    @classmethod
    def update_rankings(cls, game_id, jsondata):
        game = cls.path_to_game(game_id)
        for id in jsondata['players']:
            nickname = cls.get_nickname_from_id(game_id, id)
            chair = cls.get_user_chair(game_id, nickname)
            rank = jsondata['players'][id]['points']
            redis.jsonnumincrby('games', f'.{game}.players.{chair}.ranking', rank)
            print(redis.jsonget('games', f'.{game}.players.{chair}.ranking'))
        redis.jsonset('games', f'.{game}.any_update_in_game', True)

    @classmethod
    def any_update_in_game(cls, game_id):
        game = cls.path_to_game(game_id)
        ret = redis.jsonget('games', f'.{game}.any_update_in_game')
        redis.jsonset('games', f'.{game}.any_update_in_game', False)
        return ret

    @classmethod
    def any_userscores_to_send(cls, game_id):
        game = cls.path_to_game(game_id)
        ret = redis.jsonget('games', f'.{game}.scores_to_users')
        redis.jsonset('games', f'.{game}.scores_to_users', False)
        return ret
        

    @classmethod
    def set_status_waiting(cls, game_id):
        game = cls.path_to_game(game_id)
        redis.jsonset('games', f'.{game}.status', WAITING)
        for p in redis.jsonget('games', f'.{game}.players'):
            redis.jsonset('games', f'.{game}.players.{p}.ready', False)

    @classmethod
    def start_counting_timeout(cls, game_id, chair):
        game = cls.path_to_game(game_id)
        redis.jsonset('games',
                      f'.{game}.players.{chair}.timeout_start', time.time())

    @classmethod
    def update_times(cls, game_id):
        game = cls.path_to_game(game_id)
        for player in redis.jsonget('games', f'.{game}.players'):
            cls.update_user_time(game_id, player)

    @classmethod
    def update_user_time(cls, game_id, user=None):
        """
        if user==None -> update_current_user
        """
        game = cls.path_to_game(game_id)
        if user == cls.current_player(game_id):
            cls.update_current_user_time(game_id)

        if user is not None:
            if redis.jsonget('games', f'.{game}.players.{user}.inactive_pings') > 2:
                print('updating inactive player', user)
                finish_time = time.time()
                start_time = redis.jsonget('games',
                                           f'.{game}.players.{user}.timeout_start')
                time_delta = finish_time - start_time
                redis.jsonnumincrby('games',
                                    f'.{game}.players.{user}.timeout', -time_delta)
                redis.jsonset('games',
                              f'.{game}.players.{user}.timeout_start', finish_time)
        else:
            cls.update_current_user_time(game_id)

    @classmethod
    def update_current_user_time(cls, game_id):
        game = cls.path_to_game(game_id)
        finish_time = time.time()
        user = redis.jsonget('games', f'.{game}.current_player')
        start_time = redis.jsonget('games', f'.{game}.move_time')
        redis.jsonset('games', f'.{game}.move_time', finish_time)
        time_delta = finish_time - start_time
        redis.jsonnumincrby('games',
                            f'.{game}.players.{user}.time', -time_delta)

    @classmethod
    def get_undertime_user(cls, game_id):
        game = cls.path_to_game(game_id)
        for p, values in redis.jsonget('games', f'.{game}.players').items():
            if values['time'] <= 0:
                return values['nickname']

    @classmethod
    def get_timeouted_user(cls, game_id):
        game = cls.path_to_game(game_id)
        for p, values in redis.jsonget('games', f'.{game}.players').items():
            if values['timeout'] <= 0:
                return values['nickname']

    @classmethod
    def finish_game_by_undertime(cls, game_id):
        if cls.is_game_ongoing(game_id):
            game = cls.path_to_game(game_id)
            if cls.get_undertime_user(game_id) is not None:
                user = cls.get_undertime_user(game_id)
            elif cls.get_timeouted_user(game_id) is not None:
                user = cls.get_timeouted_user(game_id)
                redis.jsonset('games', f'.{game}.end_by_timeout', True)
            else:
                return
            cls.finish_game(game_id, [user])

    @classmethod
    def try_finish_game(cls, game_id):
        if cls.is_game_finished(game_id):
            if cls.check_if_draw(game_id):
                cls.draw_game(game_id)
            else:
                cls.choose_losers(game_id)
                losers = cls.get_losing_nicknames(game_id)
                cls.finish_game(game_id, losers)

    @classmethod
    def check_timers(cls, game_id):
        cls.update_times(game_id)
        if cls.get_undertime_user(game_id) is not None \
                or cls.get_timeouted_user(game_id) is not None:
            cls.finish_game_by_undertime(game_id)

    @classmethod
    def make_move(cls, game_id, user, action, move):
        game = cls.path_to_game(game_id)
        redis.jsonset('games', f'.{game}.state_to_send', True)
        cls.check_timers(game_id)

    @classmethod
    def is_game_drew(cls, game_id):
        game = cls.path_to_game(game_id)
        return redis.jsonget('games', f'.{game}.is_draw')

    @classmethod
    @abstractmethod
    def is_game_finished(cls, game_id):
        pass

    @classmethod
    @abstractmethod
    def check_if_draw(cls, game_id):
        pass

    @classmethod
    @abstractmethod
    def choose_losers(cls, game_id):
        "update scores.lose[]"
        pass

    @classmethod
    @abstractmethod
    def get_losing_nicknames(cls, game_id):
        """
        return [nicknames]
        """
        pass

    @classmethod
    @abstractmethod
    def possible_moves(cls, game_id):
        pass
