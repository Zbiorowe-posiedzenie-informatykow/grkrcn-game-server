from django.db import models
from django.db.models import manager
from safedelete.models import SafeDeleteModel, SOFT_DELETE
from django.contrib.postgres.fields import ArrayField
from .managers import GameManager, GameTypeManager, ParticipationManager, MoveManager


class GameType(SafeDeleteModel):
    _safedelete_policy = SOFT_DELETE
    type_name = models.CharField(max_length=50)
    description = models.CharField(max_length=1000)
    objects = GameTypeManager()

    class Meta:
        ordering = ['type_name']

    def __str__(self):
        return self.type_name


class Game(models.Model):
    # one card=two symbols; card deck=56 cards
    # chess FEN max length=90 symbols
    start_state = models.JSONField()
    game_type = models.ForeignKey(
        GameType,
        on_delete=models.CASCADE,
    )
    datetime = models.DateTimeField(auto_now=True)
    objects = GameManager()

    def __str__(self):
        return str(self.pk) + ' (' + self.game_type.type_name + ')'

    class Meta:
        ordering = ['-datetime']


class Participation(models.Model):
    class ScoreTypes(models.IntegerChoices):
        WIN = 0
        DRAW = 1
        LOSE = 2
        WIN_BY_DISCONNECT = 3
        LOSE_BY_DISCONNECT = 4
        IN_PROGRESS = 5

    user = models.PositiveIntegerField()
    game = models.ForeignKey(
        Game,
        on_delete=models.CASCADE,
    )
    score = models.IntegerField(choices=ScoreTypes.choices)
    objects = ParticipationManager()

    def __str__(self):
        return 'g' + str(self.game.pk) + '-u' + str(self.user)


class Move(models.Model):
    participation = models.ForeignKey(
        Participation,
        on_delete=models.CASCADE,
    )
    action = models.CharField(max_length=6)
    move = models.CharField(max_length=6)
    objects = MoveManager

    def __str__(self):
        return str(self.participation) + ': ' + str(self.move)
