# Bem-vindo ao
# __________         __    __  .__                               __
# \______   \_____ _/  |__/  |_|  |   ____   ______ ____ _____  |  | __ ____
#  |    |  _/\__  \   __\   __\  | _/ __ \ /  ___//    \__  \ |  |/ // __ \
#  |    |   \ / __ \|  |  |  | |  |_\  ___/ \___ \|   |  \/ __ \|    <\  ___/
#  |________/(______/__|  |__| |____/\_____>______>___|__(______/__|__\_____>
#
# ESTE É O ARQUIVO QUE VOCÊ VAI EDITAR. Todo o resto do projeto existe
# só para levar o estado do jogo até as quatro funções abaixo.
#
# Estratégia (pensada para partidas com 4+ cobras): descarta o que mata na
# hora, evita becos (flood fill) e head-to-head com cobras maiores, e só busca
# comida quando está com fome ou não é a maior da mesa.
# Documentação: https://docs.battlesnake.com

import logging
from collections import deque
from .models import GameState, MoveResponse

logger = logging.getLogger(__name__)
# O runtime Python da Lambda deixa o logger raiz em WARNING: sem esta linha
# as jogadas nao aparecem no CloudWatch.
logger.setLevel(logging.INFO)


def info() -> dict:
    """GET / — chamado quando você cadastra a cobra e a cada partida.
    Controla a aparência dela.
    Opções de cabeça, cauda e cor: https://docs.battlesnake.com/guides/customizations
    """
    logger.info("INFO")

    return {
        "apiversion": "1",
        "author": "Tokuji",
        "color": "#0077B6",  # ciano escuro / azul petróleo
        "head": "sand-worm",
        "tail": "round-bum",
        "version": "1.0.0",
    }


def start(state: GameState) -> None:
    """POST /start — chamado uma vez, quando a partida começa.
    Bom lugar para preparar qualquer estado inicial.
    """
    logger.info("JOGO COMEÇOU (partida %s)", state.game.id)


def end(state: GameState) -> None:
    """POST /end — chamado uma vez, quando a partida termina."""
    logger.info("FIM DE JOGO após %d turnos", state.turn)


MOVES = [("up", 0, 1), ("down", 0, -1), ("left", -1, 0), ("right", 1, 0)]

# Pesos da pontuação. Ordem de gravidade: beco > head-to-head > o resto.
TRAP = -1000  # espaço alcançável menor que o nosso corpo
H2H_BIGGER = -500  # casa que um adversário maior alcança junto com a gente
H2H_EQUAL = -300  # empate de tamanho: morrem os dois
H2H_SMALLER = 10  # adversário menor: o head-to-head é nosso
HAZARD = -20
HUNGER_MARGIN = 15  # vida que queremos sobrando ao chegar na comida


def _distance(a: tuple[int, int], b: tuple[int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _blocked_cells(state: GameState) -> set[tuple[int, int]]:
    """Casas ocupadas do tabuleiro neste turno."""
    blocked = set()
    for snake in [state.you, *state.board.snakes]:
        body = [(c.x, c.y) for c in snake.body]
        # A cauda sai do lugar neste turno, a não ser que a cobra tenha
        # acabado de comer (aí os dois últimos segmentos ficam empilhados).
        tail_moves = len(body) >= 2 and body[-1] != body[-2]
        blocked.update(body[:-1] if tail_moves else body)
    return blocked


# ponytail: os corpos ficam parados no BFS; casas que liberam com o tempo
# (caudas andando) não contam. Pessimista em espaço apertado.
def _bfs(start, free):
    """Gera (casa, distância) de cada casa livre alcançável a partir de `start`."""
    seen = {start}
    queue = deque([(start, 0)])
    while queue:
        cell, d = queue.popleft()
        yield cell, d
        for _, dx, dy in MOVES:
            nxt = (cell[0] + dx, cell[1] + dy)
            if nxt not in seen and free(nxt):
                seen.add(nxt)
                queue.append((nxt, d + 1))


def get_move(state: GameState) -> MoveResponse:
    """POST /move — chamado a cada turno. Aqui mora a inteligência da sua cobra.
    Precisa devolver "up", "down", "left" ou "right".
    Exemplo do JSON recebido: https://docs.battlesnake.com/api/example-move

    Cada direção que não mata na hora ganha uma nota; vence a maior.
    """
    me = state.you
    head = (me.body[0].x, me.body[0].y)
    width, height = state.board.width, state.board.height
    blocked = _blocked_cells(state)
    food = {(c.x, c.y) for c in state.board.food}
    hazards = {(c.x, c.y) for c in state.board.hazards}
    my_len = len(me.body)
    opponents = [
        ((s.body[0].x, s.body[0].y), len(s.body))
        for s in state.board.snakes
        if s.id != me.id
    ]
    biggest_opponent = max((length for _, length in opponents), default=0)
    hazard_damage = state.game.ruleset.get("settings", {}).get("hazardDamagePerTurn", 14)

    def free(cell):
        x, y = cell
        return 0 <= x < width and 0 <= y < height and cell not in blocked

    best_move, best_score = None, None
    for name, dx, dy in MOVES:
        target = (head[0] + dx, head[1] + dy)
        if not free(target):
            continue  # parede, pescoço ou corpo: morte certa
        eats = target in food
        in_hazard = target in hazards
        if in_hazard and not eats and me.health <= hazard_damage + 1:
            continue  # o hazard zera a vida

        score = 0

        for their_head, their_len in opponents:
            if _distance(their_head, target) == 1:
                if their_len > my_len:
                    score += H2H_BIGGER
                elif their_len == my_len:
                    score += H2H_EQUAL
                else:
                    score += H2H_SMALLER

        space = sum(1 for _ in _bfs(target, free))
        if space < my_len:
            score += TRAP
        # Espaço acima de 2x o corpo não faz diferença: aí quem decide é a comida.
        score += min(space, 2 * my_len)

        if in_hazard:
            score += HAZARD

        # Comida mais próxima que nenhum adversário maior/igual alcança antes.
        food_dist = next(
            (
                d
                for cell, d in _bfs(target, free)
                if cell in food
                and not any(
                    their_len >= my_len and _distance(their_head, cell) <= d + 1
                    for their_head, their_len in opponents
                )
            ),
            None,
        )
        if food_dist is not None:
            if me.health - 1 - food_dist < HUNGER_MARGIN:
                score += max(0, 200 - 5 * food_dist)  # fome: comida vira prioridade
            elif my_len <= biggest_opponent:
                score += max(0, 40 - 2 * food_dist)  # crescer para ganhar os head-to-heads

        if best_score is None or score > best_score:
            best_move, best_score = name, score

    if best_move is None:
        logger.info("MOVE %d: sem saída!", state.turn)
        return MoveResponse(move="up")
    logger.debug("MOVE %d: %s (nota %d)", state.turn, best_move, best_score)
    return MoveResponse(move=best_move)
