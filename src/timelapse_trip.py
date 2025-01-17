#!/usr/bin/env python3
import os
import shutil
import yaml
import sys
import time
import logging
import datetime as dt
import paho.mqtt.client as mqtt
from subprocess import Popen, PIPE, TimeoutExpired
from influxdb import DataFrameClient
from enums import *
from common import *
from path_files import *
from moviepy.editor import *


# ----------------------------------------------------------------------------------------------------------------------
# Read script parameters
# ----------------------------------------------------------------------------------------------------------------------
path_to_conf = os.path.join("/etc/capsule/timelapse_trip/config.yaml")
# If the default configuration is not install, then configure w/ the default one
if not os.path.exists(path_to_conf):
    sys.exit("Configuration file %s does not exists. Please reinstall the app" % path_to_conf)
# load configuration
with open(path_to_conf, "r") as file:
    conf = yaml.load(file, Loader=yaml.FullLoader)

# ----------------------------------------------------------------------------------------------------------------------
# Initiate variables
# ----------------------------------------------------------------------------------------------------------------------
connected = False
logging.basicConfig(
    filename="/var/log/capsule/timelapse_trip.log",
    filemode="a",
    level=logging.DEBUG if conf["debug"] else logging.INFO,
    format="%(asctime)s %(levelname)s:%(message)s",
    datefmt='%m/%d/%Y %I:%M:%S %p')

# Dump configuration
logging.debug("Dump configuration: " + repr(conf))

# ----------------------------------------------------------------------------------------------------------------------
# Initiate MQTT variables
# ----------------------------------------------------------------------------------------------------------------------
motion_state = Motion.IDLE # By default
time_since_idle = None
stop_command = True
ignition = False
car_moving = False
# MQTT methods
def on_connect(client, userdata, flags, rc):  # The callback for when the client connects to the broker
    logging.info("Connected with result code {0}".format(str(rc)))  # Print result of connection attempt

    for topic in ["router/car/moving", "router/car/running", "timelapse_trip/stop_command", "router/gps/latitude", "router/gps/longitude"]:
        r=client.subscribe(topic)
        tries = 10
        while r[0]!=0:
            logging.info("Waiting to subscribe to " + topic + " / Will exit the process after " + tries + " tries")
            time.sleep(0.5)
            if tries <= 0:
                logging.info("Stop script")
                client.publish("process/timelapse_trip/alive", False)
                client.loop_stop()
                client.disconnect()
                sys.exit(1)

def on_message(client, userdata, msg):  # The callback for when a PUBLISH message is received from the server.
    global ignition, car_moving, stop_command, motion_state, time_since_idle

    def update_state(state: Motion):
        global time_since_idle, motion_state
        if isinstance(state, Motion):
            if state != motion_state:
                logging.info("Change app state from " + repr(state) + " to " + repr(motion_state) + " succeeded")
                motion_state = state
                if state == Motion.IDLE:
                    time_since_idle = dt.datetime.now()
                else:
                    time_since_idle = None
        else:
            logging.warning("Change app state from " + repr(state) + " to " + repr(motion_state) + " failed")

    data = msg.payload.decode("utf-8")
    if msg.topic == "timelapse_trip/stop_command":
        stop_command = True if data == "True" else False
    if msg.topic == "router/car/running":
        ignition = True if data == "1" else False
    if msg.topic == "router/car/moving":
        car_moving = True if data == "1" else False

    if msg.topic == "router/car/running" or msg.topic == "router/car/moving":
        if ignition and car_moving:
            # The ignition is on and the car is moving
            update_state(Motion.DRIVE)
        if not ignition and not car_moving:
            update_state(Motion.STOP)
        if ignition and not car_moving:
            # The car is on but not moving
            update_state(Motion.IDLE)



def wait_for(client,msgType,period=0.25):
 if msgType=="SUBACK":
  if client.on_subscribe:
    while not client.suback_flag:
      logging.info("waiting suback")
      client.loop()  #check for messages
      time.sleep(period)

client = mqtt.Client()
logging.info("Connect to localhost broker")
client.username_pw_set(conf["mqtt"]["user"], conf["mqtt"]["pass"])
client.on_connect = on_connect  # Define callback function for successful connection
client.on_message = on_message
client.connect(conf["mqtt"]["host"], conf["mqtt"]["port"])
client.loop_start()

while not client.is_connected():
    logging.info("Waiting for the broker connection")
    time.sleep(1)

# ----------------------------------------------------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------------------------------------------------
def stop_script(process, why):
    if not process:
        return
    logging.info("Process killed: " + why)
    process.stdin.write("q")
    process.communicate()[0]
    process.stdin.close()
    logging.info("Wait 5 seconds for the subprocess to be correctly killed")
    time.sleep(5)


process = None
try:
    while True:
        client.publish("process/timelapse_trip/alive", True)
        client.publish("process/timelapse_trip/last_status", "Waiting for action")
        if motion_state == Motion.DRIVE and not stop_command:
            args = ["/home/rudloff/sources/CapsuleScripts/timelapse_geolocate/src/ffmpeg_timelapse_thread.sh", str(conf["rtsp"]["framerate"])]
            process = Popen(args, stdin=PIPE, stdout=PIPE, stderr=PIPE, shell=True, encoding='utf8')
            try:
                logging.info("Start timelapse")

                logging.info("Start camera if not ON")
                os.system("curl http://192.168.10.1/cgi-bin/io_state\?username\=rudloff\&password\=pam1249CS1110ragot\&pin\=dout2\&state\=on")
                pic = 0
                while True:
                    pic = pic + 1
                    client.publish("process/timelapse_trip/alive", True)
                    client.publish("process/timelapse_trip/last_status", "Take picture")
                    client.publish("process/timelapse_trip/timelapse_process_progress", pic)
                    if motion_state == Motion.STOP or stop_command:
                        stop_script(process, "Motion Stop" if motion_state == Motion.STOP else "stop command by user")
                        break
                    # OR if idling for too long
                    if time_since_idle:
                        if motion_state == Motion.IDLE and dt.datetime.now() - time_since_idle > dt.timedelta(seconds=10):
                            stop_script(process, "Idling for too long")
                            break
                    time.sleep(1/conf["rtsp"]["framerate"])
            except TimeoutExpired as e:
                stop_script(process, 'Timelapse process as been killed outside of the script: {}'.format(e))
            except KeyboardInterrupt:
                stop_script(process, "Timelapse process as been killed by the user")
                pass
        # Get the non empty timelapses that need to be processed
        timelapses_to_process = get_timelapse_to_process(path_to_timelapse_tmp, path_to_current_results)
        if timelapses_to_process: # If the list is not empty
            client.publish("process/timelapse_trip/last_status", "Generate timelapse")
            for timelapse_to_process in timelapses_to_process:
                logging.info("Start process the timelapse:" + timelapse_to_process)
                client.publish("process/timelapse_trip/timelapse_process_progress", 0)
                # Retrieve the timestamps
                images_sorted = sorted(os.listdir(timelapse_to_process))
                # Generate video for at least 5 minutes of images (300 images)
                if len(images_sorted) < 300:
                    logging.warning("The timelapse is too short (" + str(len(images_sorted)) + " images). The timelapse is not generated")
                    #shutil.rmtree(timelapse_to_process)
                    shutil.move(timelapse_to_process, f"/home/rudloff/timelapse_backup/{dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")}/photo")
                    break

                # Keep only jpg files
                images_sorted = [image for image in images_sorted if image.endswith(".jpg")]
                timestamps = [datetime.strptime(timestamp_dirty.strip(".jpg"), '%Y-%m-%d_%H-%M-%S').isoformat()\
                     for timestamp_dirty in images_sorted]
                client.publish("process/timelapse_trip/timelapse_process_progress", 10)

                # Connect to influxdb client
                #client = DataFrameClient("localhost", "8086", "rudloff", "y4uv3jpc", "telegraf")
                influxdb_client =  DataFrameClient(conf["influxdb"]["url"], conf["influxdb"]["port"], conf["influxdb"]["user"], conf["influxdb"]["pass"], conf["influxdb"]["database"])
                # Get the list of gps coordinates from the influxdb database
                gps_coords = pd.DataFrame(retrieve_lat_lon(timestamps, influxdb_client))
                # fill the N/A values with previous values
                gps_coords.fillna(method='ffill', inplace=True)
                gps_coords.fillna(method='bfill', inplace=True) # Fill in the first value
                continue_without_map = False
                if gps_coords.empty:
                    logging.warning("The gps coordinates corresponding are not retrieved in the influxdb database. The timelapse generate continues without the map")
                    continue_without_map = True
                if gps_coords.isnull().values.any(): # Still got NaN values
                    logging.warning("The gps coordinates corresponding are still null. The timelapse generation continues frames without the map")
                    continue_without_map = True
                continue_without_map = True

                # Construct the map
                if conf["map_generation_mean"] == "local":
                    tiles = tilemapbase.tiles.Tiles(conf["map_generation"]["url"], conf["map_generation"]["tile_name"], headers={"User-Agent":"TileMapBase"})
                else:
                    tiles = tilemapbase.tiles.build_OSM()

                # Create the map folder
                path_to_maps = os.path.join(timelapse_to_process, "maps")
                if os.path.exists(path_to_maps):
                    #shutil.rmtree(path_to_maps)
                    shutil.move(timelapse_to_process, f"/home/rudloff/timelapse_backup/{dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")}maps")
                os.makedirs(path_to_maps, 0o740)
                os.chown(path_to_maps, 1000, 1000) # Rudloff id and group Root
                os.chmod(path_to_maps, 0o775) # Give all read access but Rudloff write access
                lat_list = []
                lon_list = []
                if not continue_without_map:
                    for lat, lon, timestamp in zip(gps_coords["latitude"].tolist(), gps_coords["longitude"].tolist(), gps_coords.index) :
                        client.publish("process/timelapse_trip/timelapse_process_progress", 10+round(50*len(lat_list)/len(gps_coords["latitude"].tolist())))
                        lat_list.append(lat)
                        lon_list.append(lon)
                        # Retrieve the maps and save it
                        retrieve_save_map(lat_list, lon_list, tiles, datetime.strftime(timestamp, '%Y-%m-%d_%H-%M-%S'), path_to_maps)
                        time.sleep(0.01)

                # Combine the map and the frame and generate the mp4 timelapse
                frameSize = (2304, 1296)
                result_folder = path_to_current_results + "/" + os.path.basename(timelapse_to_process)
                os.makedirs(result_folder, 0o740)
                video_out = cv2.VideoWriter(result_folder + "/video.mp4",
                cv2.VideoWriter_fourcc(*'mp4v'), 10, frameSize)
                if not continue_without_map:
                    for idx, timestamp in enumerate(gps_coords.index):
                        timestamp_date = datetime.strftime(timestamp, '%Y-%m-%d_%H-%M-%S')
                        client.publish("process/timelapse_trip/timelapse_process_progress", 10+50+round(20*idx/len(images_sorted)))
                        video_frame = combine(os.path.join(path_to_maps,timestamp_date)+".png", os.path.join(timelapse_to_process, timestamp_date)+".jpg",
                                            timestamp_date, gps_coords["latitude"].values[idx], gps_coords["longitude"].values[idx])
                        if video_frame is not None:
                            video_out.write(video_frame)
                        time.sleep(0.01)
                else:
                    for idx, image_path in enumerate(images_sorted):
                        video_frame = cv2.imread(os.path.join(timelapse_to_process, image_path))
                        if video_frame is not None:
                            video_out.write(video_frame)
                video_out.release()
                logging.info("Timelapse saved: " + result_folder + "/video.mp4")

                # Remove the maps folder
                if os.path.exists(path_to_maps):
                    #shutil.rmtree(path_to_maps)
                    shutil.move(timelapse_to_process, f"/home/rudloff/timelapse_backup/{dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")}/map")
                    logging.info("Remove maps folder")
                client.publish("process/timelapse_trip/timelapse_process_progress", 100)

                break
                # TODO convert in GIF (but for now its generation is heavy in terms off ram and memory)
                # Combine the map and the frame and generate the gif timelapse
                clip = (VideoFileClip(result_folder + "/video.mp4"))
                clip.write_gif(result_folder + "/video.gif")
                clip.write_gif()
                client.publish("process/timelapse_trip/timelapse_process_progress", 100)
                logging.info("Timelapse saved: " + result_folder + "/video.gif")
                time.sleep(0.1)
        # Clear empty tmp folder or already generated timelapses
        # TODO for now the timelapses are not removed
        #clear_timelapse_to_process(path_to_timelapse_tmp, path_to_current_results)
        time.sleep(1)
except KeyboardInterrupt:
    stop_script(process, "Timelapse process as been killed by the user")
    pass

logging.info("Stop script")
client.publish("process/timelapse_trip/alive", False)
client.loop_stop()
client.disconnect()
sys.exit(0)
