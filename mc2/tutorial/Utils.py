from pymongo import MongoClient
from os.path import expanduser
import subprocess
import sys
import xgboost as xgb
from numpy import genfromtxt
import logging
import pickle
import pandas as pd

db_username = "xgboost"
db_password = "risecampmc2"


class PKI:
    def __init__(self):
        self.client = MongoClient(
            'mongodb://%s:%s@54.202.14.46:27017/PKI' % (db_username, db_password))
        self.db = self.client['PKI']

    def upload(self, username, IP, pubkey):
        try:
            collection = self.db.posts
            query = \
                {
                    'user': username
                }
            doc = \
                {
                    'user': username,
                    'IP': IP,
                    'key': pubkey
                }
            result = collection.find_one_and_replace(query, doc, upsert=True)
            print(result)
        except Exception as e:
            print(str(e))

    def lookup(self, username):
        try:
            posts = self.db.posts
            query = {
                'user': username
            }
            result = posts.find_one(query)
            if result == None:
                print("User %s not found" % username)
                return None, None
            else:
                return result['IP'], result['key']
        except Exception as e:
            print(str(e))

    def save_key(self, username):
        try:
            IP, key = self.lookup(username)
            if key == None:
                return
            home = expanduser("~")
            with open(home + "/.ssh/authorized_keys", "a") as authorized_keys:
                authorized_keys.write("%s\n" % key)
                print("Saved key for user %s" % username)
        except Exception as e:
            print(str(e))


class Federation:
    def __init__(self, username):
        self.client = MongoClient(
            'mongodb://%s:%s@54.202.14.46:27017/Federations' % (db_username, db_password))
        self.db = self.client['Federations']
        self.username = username
        self.aggregator = None

    def check_federation(self):
        if self.aggregator is None:
            print("No federation to check. Please create or join a federation first.")
            return

        try:
            collection = self.db.federations
            query = \
                {
                    'master': self.aggregator,
                }
            result = collection.find_one(query)
            if result == None:
                print("No such federation exists")
                return False

            members = result['members']
            print("Federation members: %s" % members)

            collection = self.db.members
            for member in members:
                query = \
                    {
                        'member': member,
                        'federation': self.aggregator
                    }
                if collection.count_documents(query, limit=1) == 0:
                    print("User %s has not joined the federation" % member)
                    return False
            print("All users have joined the federation")
            return True
        except Exception as e:
            print(str(e))


class FederationAggregator(Federation):
    def __init__(self, username):
        Federation.__init__(self, username)
        self.aggregator = username

    def create_federation(self, members):
        try:
            if self.username not in members:
                members.append(self.username)

            collection = self.db.federations
            query = \
                {
                    'master': self.username
                }
            doc = \
                {
                    'master': self.username,
                    'members': members
                }
            result = collection.find_one_and_replace(query, doc, upsert=True)
            print(result)

            collection = self.db.members
            query = \
                {
                    'member': self.username
                }
            doc = \
                {
                    'member': self.username,
                    'federation': self.username,
                    'role': 'master'
                }
            result = collection.find_one_and_replace(query, doc, upsert=True)
        except Exception as e:
            print(str(e))


class FederationMember(Federation):
    def __init__(self, username):
        Federation.__init__(self, username)

    def join_federation(self, master_username):
        try:

            collection = self.db.federations
            query = \
                {
                    'master': master_username,
                    'members': {'$all': [self.username]}

                }
            result = collection.find_one(query)
            if result == None:
                print(
                    "Either the federation does not exist, or the central server (aggregator) hasn't added you as a member.")
                return

            self.aggregator = master_username

            collection = self.db.members
            query = \
                {
                    'member': self.username
                }
            doc = \
                {
                    'member': self.username,
                    'federation': master_username,
                    'role': 'worker'
                }
            result = collection.find_one_and_replace(query, doc, upsert=True)
            print(result)

        except Exception as e:
            print(str(e))


class FederatedXGBoost:
    def __init__(self):
        xgb.rabit.init()
        self.dtrain = None
        self.dtest = None
        self.model = None

    def load_training_data(self, training_data_path):
        training_data_path = training_data_path[:-4] + \
            "_" + str(xgb.rabit.get_rank() + 1) + ".csv"
        training_data = genfromtxt(training_data_path, delimiter=',')
        self.dtrain = xgb.DMatrix(
            training_data[:, 1:], label=training_data[:, 0])

    def load_test_data(self, test_data_path):
        test_data_path = test_data_path[:-4] + \
            "_" + str(xgb.rabit.get_rank() + 1) + ".csv"
        test_data = genfromtxt(test_data_path, delimiter=',')
        self.dtest = xgb.DMatrix(test_data[:, 1:], label=test_data[:, 0])

    def train(self, params, num_rounds):
        if self.dtrain == None:
            print("Training data not yet loaded")
        self.model = xgb.train(params, self.dtrain, num_rounds)

    def predict(self):
        if self.dtest == None:
            print("Test data not yet loaded")
        return self.model.predict(self.dtest)

    def eval(self):
        if self.dtest == None:
            print("Test data not yet loaded")
        return self.model.eval(self.dtest)

    def get_num_parties(self):
        return xgb.rabit.get_world_size()

    def load_model(self, model_path):
        self.model = xgb.Booster()
        self.model.load_model(model_path)

    def save_model(self, model_name):
        self.model.save_model(model_name)
        print("Saved model to {}".format(model_name))

    def shutdown(self):
        print("Shutting down tracker")
        xgb.rabit.finalize()


def start_job(num_parties):
    # Check if training_job is already running on each machine; if so kill it
    with open("hosts.config") as f:
        tmp = f.readlines()
    for h in tmp:
        if len(h.strip()) > 0:
            # parse addresses of the form ip:port
            h = h.strip()
            i = h.find(":")
            p = "22"
            if i != -1:
                p = h[i+1:]
                h = h[:i]
           
           kill_cmd = "kill -9 $(ps aux | awk '$12 $28 ~ \"train_model.py\" {print $2}')"
            ssh_kill_cmd = ["ssh", "-o", "StrictHostKeyChecking=no", str(h), "-p", str(p), kill_cmd]
           
           process = subprocess.Popen(
                ssh_kill_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            # for line in iter(process.stdout.readline, b''):
            #    sys.stdout.write(line)

    cmd = ["../dmlc-core/tracker/dmlc-submit", "--cluster", "ssh", "--num-workers",
           str(num_parties), "--host-file", "hosts.config", "--worker-memory", "4g", "/opt/conda/bin/python3", "/home/$USER/train_model.py"]
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for line in iter(process.stdout.readline, b''):
        sys.stdout.write(line)




