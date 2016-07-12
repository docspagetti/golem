from multiprocessing import Process, Queue
from os import path

from gnr.ui.gen.ui_BlenderWidget import Ui_BlenderWidget
from gnr.ui.gen.ui_LuxWidget import Ui_LuxWidget
from twisted.internet.defer import inlineCallbacks

from gnr.application import GNRGui
from gnr.customizers.blenderrenderdialogcustomizer import BlenderRenderDialogCustomizer
from gnr.customizers.luxrenderdialogcustomizer import LuxRenderDialogCustomizer
from gnr.customizers.renderingmainwindowcustomizer import RenderingMainWindowCustomizer
from gnr.renderingapplicationlogic import RenderingApplicationLogic
from gnr.renderingenvironment import BlenderEnvironment, LuxRenderEnvironment
from gnr.task.blenderrendertask import build_blender_renderer_info
from gnr.task.luxrendertask import build_lux_render_info
from gnr.ui.appmainwindow import AppMainWindow
from gnr.ui.widget import TaskWidget
from golem.client import Client
from golem.core.common import get_golem_path
from golem.core.processmonitor import ProcessMonitor
from golem.environments.environment import Environment
from golem.rpc.service import RPCServiceInfo
from golem.rpc.websockets import WebSocketRPCServerFactory, WebSocketRPCClientFactory

LOG_NAME = "golem.log"


def config_logging(logger_name, logname=LOG_NAME):
    """Config logger"""
    import logging.config
    config_file = path.normpath(path.join(get_golem_path(), "gnr", "logging.ini"))
    logging.config.fileConfig(config_file, defaults={'logname': logname}, disable_existing_loggers=False)
    return logging.getLogger(logger_name)


def install_qt4_reactor():
    try:
        import qt4reactor
    except ImportError:
        # Maybe qt4reactor is placed inside twisted.internet in site-packages?
        from twisted.internet import qt4reactor
    qt4reactor.install()
    from twisted.internet import reactor
    return reactor


def stop_reactor():
    from twisted.internet import reactor
    if reactor.running:
        reactor.stop()


def load_environments():
    return [LuxRenderEnvironment(),
            BlenderEnvironment(),
            Environment()]


def register_rendering_task_types(logic):
    logic.register_new_renderer_type(build_blender_renderer_info(TaskWidget(Ui_BlenderWidget),
                                                                 BlenderRenderDialogCustomizer))
    logic.register_new_renderer_type(build_lux_render_info(TaskWidget(Ui_LuxWidget),
                                                           LuxRenderDialogCustomizer))


class GUIApp(object):

    def __init__(self, rendering):
        self.logic = RenderingApplicationLogic()
        self.app = GNRGui(self.logic, AppMainWindow)
        self.logic.register_gui(self.app.get_main_window(),
                                RenderingMainWindowCustomizer)
        self.client = None

        if rendering:
            register_rendering_task_types(self.logic)

    @inlineCallbacks
    def start(self, client, logic_service_info):
        self.client = client
        yield self.logic.register_client(self.client, logic_service_info)
        yield self.logic.start()
        yield self.logic.check_network_state()
        self.app.execute(True)


def start_gui_process(queue, datadir, rendering=True, gui_app=None, reactor=None):

    logger_name = "gnr.app"
    if datadir:
        logger = config_logging(logger_name, path.join(datadir, LOG_NAME))
    else:
        logger = config_logging(logger_name)

    client_service_info = queue.get(True, 3600)

    if not isinstance(client_service_info, RPCServiceInfo):
        logger.error("GUI process error: {}".format(client_service_info))
        return

    if not gui_app:
        gui_app = GUIApp(rendering)
    if not reactor:
        reactor = install_qt4_reactor()

    rpc_address = client_service_info.rpc_address
    rpc_client = WebSocketRPCClientFactory(rpc_address.host, rpc_address.port)

    def on_connected(_):
        golem_client = rpc_client.build_client(client_service_info)
        logic_service_info = rpc_client.add_service(gui_app.logic)
        gui_app.start(client=golem_client, logic_service_info=logic_service_info)

    def on_error(error):
        if reactor.running:
            reactor.stop()
        logger.error("GUI process error: {}".format(error))

    def connect():
        rpc_client.connect().addCallbacks(on_connected, on_error)

    reactor.callWhenRunning(connect)
    if not reactor.running:
        reactor.run()


def start_client_process(queue, start_ranking, datadir=None,
                         transaction_system=False, client=None):

    logger_name = "golem.client"
    if datadir:
        logger = config_logging(logger_name, path.join(datadir, LOG_NAME))
    else:
        logger = config_logging(logger_name)

    environments = load_environments()

    if not client:
        try:
            client = Client(datadir=datadir, transaction_system=transaction_system)
            client.start()
        except Exception as exc:
            logger.error("Client process error: {}".format(exc))
            queue.put(exc)
            return

    for env in environments:
        client.environments_manager.add_environment(env)
    client.environments_manager.load_config(client.datadir)

    def listen():
        rpc_server = WebSocketRPCServerFactory()
        rpc_server.listen()

        client_service_info = client.set_rpc_server(rpc_server)

        queue.put(client_service_info)
        queue.close()

    from twisted.internet import reactor

    if start_ranking:
        client.ranking.run(reactor)

    reactor.callWhenRunning(listen)
    if not reactor.running:
        reactor.run()


def start_app(datadir=None, rendering=False,
              start_ranking=True, transaction_system=False):

    queue = Queue()

    gui_process = Process(target=start_gui_process,
                          args=(queue, datadir, rendering))
    gui_process.daemon = True
    gui_process.start()

    process_monitor = ProcessMonitor(gui_process)
    process_monitor.add_shutdown_callback(stop_reactor)
    process_monitor.start()

    try:
        start_client_process(queue, start_ranking, datadir, transaction_system)
    except Exception as exc:
        print "Exception in Client process: {}".format(exc)

    process_monitor.exit()
