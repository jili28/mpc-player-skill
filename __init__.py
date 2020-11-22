from mycroft import MycroftSkill, intent_file_handler


class MpcPlayer(MycroftSkill):
    def __init__(self):
        MycroftSkill.__init__(self)

    @intent_file_handler('player.mpc.intent')
    def handle_player_mpc(self, message):
        self.speak_dialog('player.mpc')


def create_skill():
    return MpcPlayer()

