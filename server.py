import asyncio
import json
import os
import re

from aiohttp import web, WSMsgType

PORT = int(os.environ.get('PORT', 3000))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HANDLE_RE = re.compile(r'^[\w\s\-]{2,15}$')


class OthelloGame:
    DIRS = [(0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1)]

    def __init__(self, handles, websockets, lobby):
        self.handles = handles      # {1: 'Alice', 2: 'Bob'}
        self.ws = websockets        # {1: ws1, 2: ws2}
        self.lobby = lobby
        self.board = [[0] * 8 for _ in range(8)]
        self.board[3][3] = 1
        self.board[4][4] = 1
        self.board[3][4] = 2
        self.board[4][3] = 2
        self.turn = 1
        self.over = False
        self._ended = False

    def _valid_moves_for(self, player):
        return [(x, y) for y in range(8) for x in range(8)
                if self._is_valid(x, y, player)]

    def _is_valid(self, x, y, player):
        if self.board[y][x] != 0:
            return False
        opp = 3 - player
        for dx, dy in self.DIRS:
            nx, ny, cnt = x + dx, y + dy, 0
            while 0 <= nx < 8 and 0 <= ny < 8 and self.board[ny][nx] == opp:
                nx += dx
                ny += dy
                cnt += 1
            if cnt > 0 and 0 <= nx < 8 and 0 <= ny < 8 and self.board[ny][nx] == player:
                return True
        return False

    def _apply(self, x, y, player):
        opp = 3 - player
        self.board[y][x] = player
        flipped = []
        for dx, dy in self.DIRS:
            nx, ny = x + dx, y + dy
            line = []
            while 0 <= nx < 8 and 0 <= ny < 8 and self.board[ny][nx] == opp:
                line.append((nx, ny))
                nx += dx
                ny += dy
            if line and 0 <= nx < 8 and 0 <= ny < 8 and self.board[ny][nx] == player:
                for fx, fy in line:
                    self.board[fy][fx] = player
                flipped.extend(line)
        return flipped

    def _scores(self):
        s1 = sum(c == 1 for r in self.board for c in r)
        s2 = sum(c == 2 for r in self.board for c in r)
        return {'1': s1, '2': s2}

    def _make_state(self, last_move=None, flipped=None, passed_player=None):
        vm = self._valid_moves_for(self.turn)
        return {
            'type': 'state',
            'board': self.board,
            'turn': self.turn,
            'scores': self._scores(),
            'valid_moves': [[x, y] for x, y in vm],
            'last_move': last_move,
            'flipped': [[x, y] for x, y in flipped] if flipped else [],
            'passed_player': passed_player,
        }

    async def _broadcast(self, msg):
        for ws in self.ws.values():
            if not ws.closed:
                await ws.send_json(msg)

    async def start(self):
        await self._broadcast(self._make_state())

    async def handle_move(self, x, y, player):
        if self.turn != player or self.over:
            return
        if not self._is_valid(x, y, player):
            return

        flipped = self._apply(x, y, player)
        last_move = {'x': x, 'y': y, 'player': player}

        next_p = 3 - player
        passed = None

        vm_next = self._valid_moves_for(next_p)
        vm_curr = self._valid_moves_for(player)

        if vm_next:
            self.turn = next_p
        elif vm_curr:
            passed = next_p
        else:
            self.over = True
            await self._broadcast(self._make_state(last_move, flipped))
            await asyncio.sleep(3)
            await self._end_game()
            return

        if all(self.board[r][c] != 0 for r in range(8) for c in range(8)):
            self.over = True
            await self._broadcast(self._make_state(last_move, flipped))
            await asyncio.sleep(3)
            await self._end_game()
            return

        await self._broadcast(self._make_state(last_move, flipped, passed))

    async def handle_disconnect(self, disconnected_color):
        if self.over:
            return
        self.over = True
        other = 3 - disconnected_color
        other_ws = self.ws.get(other)
        if other_ws and not other_ws.closed:
            await other_ws.send_json({'type': 'opponent_left'})
        await asyncio.sleep(2)
        await self.lobby.game_ended(self)

    async def _end_game(self):
        sc = self._scores()
        if sc['1'] > sc['2']:
            winner = self.handles[1]
        elif sc['2'] > sc['1']:
            winner = self.handles[2]
        else:
            winner = None

        await self._broadcast({
            'type': 'gameover',
            'scores': sc,
            'winner': winner,
            'handles': {'1': self.handles[1], '2': self.handles[2]},
        })

        await asyncio.sleep(5)
        await self.lobby.game_ended(self)


class Lobby:
    def __init__(self):
        self.players = {}     # handle → {'ws': ws, 'game': game|None}
        self.challenges = {}  # challenger → target

    def _handle_for(self, ws):
        for h, p in self.players.items():
            if p['ws'] is ws:
                return h
        return None

    async def _broadcast_lobby(self):
        idle = [h for h, p in self.players.items() if p['game'] is None]
        for handle, player in list(self.players.items()):
            if player['game'] is not None:
                continue
            ws = player['ws']
            if ws.closed:
                continue
            incoming = [c for c, t in self.challenges.items() if t == handle]
            outgoing = self.challenges.get(handle)
            await ws.send_json({
                'type': 'lobby_state',
                'players': [{'handle': h} for h in idle],
                'outgoing': outgoing,
                'incoming': incoming,
            })

    async def handle_message(self, ws, data):
        t = data.get('type')

        if t == 'join_lobby':
            handle = data.get('handle', '').strip()
            if not HANDLE_RE.match(handle):
                await ws.send_json({'type': 'error',
                                    'message': 'Name must be 2–15 chars (letters, numbers, spaces, hyphens)'})
                return
            if handle in self.players:
                await ws.send_json({'type': 'error', 'message': 'Name already taken'})
                return
            self.players[handle] = {'ws': ws, 'game': None}
            await ws.send_json({'type': 'joined_lobby', 'handle': handle})
            await self._broadcast_lobby()

        elif t == 'challenge':
            challenger = self._handle_for(ws)
            target = data.get('target')
            if (challenger and target and target in self.players
                    and self.players[target]['game'] is None
                    and target != challenger):
                self.challenges[challenger] = target
                await self._broadcast_lobby()

        elif t == 'cancel_challenge':
            challenger = self._handle_for(ws)
            if challenger and challenger in self.challenges:
                del self.challenges[challenger]
                await self._broadcast_lobby()

        elif t == 'accept':
            accepter = self._handle_for(ws)
            challenger = data.get('challenger')
            if (accepter and challenger
                    and challenger in self.players
                    and self.challenges.get(challenger) == accepter
                    and self.players[accepter]['game'] is None
                    and self.players[challenger]['game'] is None):
                await self._start_game(challenger, accepter)

        elif t == 'decline':
            decliner = self._handle_for(ws)
            challenger = data.get('challenger')
            if (decliner and challenger
                    and challenger in self.players
                    and self.challenges.get(challenger) == decliner):
                del self.challenges[challenger]
                c_ws = self.players[challenger]['ws']
                if not c_ws.closed:
                    await c_ws.send_json({'type': 'challenge_declined', 'by': decliner})
                await self._broadcast_lobby()

        elif t == 'move':
            handle = self._handle_for(ws)
            if not handle:
                return
            player = self.players.get(handle)
            if not player or not player['game']:
                return
            game = player['game']
            color = 1 if game.handles[1] == handle else 2
            x = data.get('x')
            y = data.get('y')
            if isinstance(x, int) and isinstance(y, int):
                await game.handle_move(x, y, color)

    async def _start_game(self, p1_handle, p2_handle):
        for h in list(self.challenges):
            if self.challenges[h] in (p1_handle, p2_handle) or h in (p1_handle, p2_handle):
                del self.challenges[h]

        ws1 = self.players[p1_handle]['ws']
        ws2 = self.players[p2_handle]['ws']

        game = OthelloGame(
            handles={1: p1_handle, 2: p2_handle},
            websockets={1: ws1, 2: ws2},
            lobby=self,
        )

        self.players[p1_handle]['game'] = game
        self.players[p2_handle]['game'] = game

        await ws1.send_json({
            'type': 'game_start',
            'your_color': 1,
            'handles': {'1': p1_handle, '2': p2_handle},
        })
        await ws2.send_json({
            'type': 'game_start',
            'your_color': 2,
            'handles': {'1': p1_handle, '2': p2_handle},
        })

        await game.start()

    async def game_ended(self, game):
        if game._ended:
            return
        game._ended = True
        for color in (1, 2):
            handle = game.handles[color]
            if handle in self.players:
                self.players[handle]['game'] = None
                ws = self.players[handle]['ws']
                if not ws.closed:
                    await ws.send_json({'type': 'returned_to_lobby'})
        await self._broadcast_lobby()

    async def handle_disconnect(self, ws):
        handle = self._handle_for(ws)
        if not handle:
            return

        player = self.players[handle]
        game = player['game']

        if game and not game.over:
            color = 1 if game.handles[1] == handle else 2
            await game.handle_disconnect(color)

        self.challenges.pop(handle, None)
        for h in list(self.challenges):
            if self.challenges.get(h) == handle:
                del self.challenges[h]

        self.players.pop(handle, None)
        await self._broadcast_lobby()


lobby = Lobby()


async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    await lobby.handle_message(ws, data)
                except Exception:
                    pass
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        await lobby.handle_disconnect(ws)
    return ws


async def index_handler(request):
    raise web.HTTPFound('/othello.html')


app = web.Application()
app.router.add_get('/', index_handler)
app.router.add_get('/ws', ws_handler)
app.router.add_static('/', path=BASE_DIR, name='static', show_index=False)

if __name__ == '__main__':
    web.run_app(app, port=PORT)
