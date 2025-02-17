# Optuna MLP Trial Class for hyperparameter optimisation.
# See the mlp.py file for detailed information about the architecture.

import os
import numpy as np

# Silence the info from tensorflow in which it brags that it can run on cpu nicely.
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "1"
import optuna
import sklearn
import tensorflow as tf
import tensorflow.keras.layers as KL
from tensorflow import keras


import absl.logging

absl.logging.set_verbosity(absl.logging.ERROR)

import util.util
import util.data
from util.terminal_colors import tcols
from . import util as dsutil


def main(args):
    util.util.device_info()
    outdir = util.util.make_output_directory("trained_mlps", args["outdir"])

    data = util.data.Data.load_kfolds(**args["data_hyperparams"])

    study = optuna.create_study(
        study_name=args["study_name"],
        sampler=optuna.samplers.TPESampler(),
        pruner=optuna.pruners.HyperbandPruner(),
        direction="maximize",
        storage=f"sqlite:///{outdir}/{args['storage']}.db",
        load_if_exists=True,
    )
    study.optimize(Objective(data, args), n_trials=250, gc_after_trial=True)


class Objective:
    def __init__(self, jet_data: util.data.Data, args: dict):
        self.jet_data = jet_data
        self.args = args
        self.training_hyperparams = {
            "epochs": args["training_hyperparams"]["epochs"],
        }
        self.compilation_hyperparams = {
            "loss": args["compilation"]["loss"],
            "metrics": args["compilation"]["metrics"],
        }
        self.model_hyperparams = {}

    def __call__(self, trial):
        tf.keras.backend.clear_session()
        self.training_hyperparams.update(
            {
                "batch": trial.suggest_categorical(
                    "bs", self.args["training_hyperparams"]["batch"]
                ),
                "lr": trial.suggest_float(
                    "lr", *self.args["training_hyperparams"]["lr"], log=True
                ),
            }
        )
        self.compilation_hyperparams.update(
            {
                "optimizer": trial.suggest_categorical(
                    "optim", self.args["compilation"]["optimizer"]
                ),
            }
        )

        nlayers = trial.suggest_categorical(
            "nlayers", self.args["model_hyperparams"]["nlayers"]
        )

        layers = []
        if self.jet_data.ncons == 8:
            layers.append(120)
            layers.append(60)
        if self.jet_data.ncons == 16:
            layers.append(88)
        if self.jet_data.ncons == 32:
            layers.append(84)

        for layer in range(nlayers - len(layers)):
            layers.append(
                trial.suggest_categorical(
                    f"layer{layer}_nodes", self.args["model_hyperparams"]["nnodes"]
                )
            )

        self.model_hyperparams.update(
            {
                "layers": layers,
                "l1_coeff": trial.suggest_float(
                    "l1_coeff", *self.args["model_hyperparams"]["l1_coeff"], log=True
                ),
                # "dropout_rate": trial.suggest_float(
                # "dropout_rate", *self.args["model_hyperparams"]["dropout_rate"]
                # ),
                "activ": trial.suggest_categorical(
                    "activ", self.args["model_hyperparams"]["activ"]
                ),
                "nbits": -1,
            }
        )

        model = dsutil.choose_mlp(
            self.args["mlp_type"],
            self.jet_data.ntrain_jets,
            self.jet_data.ncons,
            self.jet_data.nfeat,
            self.model_hyperparams,
            self.compilation_hyperparams,
            self.training_hyperparams,
        )
        model.summary(expand_nested=True)

        print(tcols.HEADER + "\n\nTRAINING THE MODEL \U0001F4AA" + tcols.ENDC)

        callbacks = get_tensorflow_callbacks()
        callbacks.append(
            optuna.integration.TFKerasPruningCallback(trial, "val_categorical_accuracy")
        )

        model.fit(
            self.jet_data.train_data,
            self.jet_data.train_target,
            epochs=self.training_hyperparams["epochs"],
            batch_size=self.training_hyperparams["batch"],
            verbose=2,
            callbacks=callbacks,
            validation_data=(self.jet_data.test_data, self.jet_data.test_target),
            shuffle=True,
        )

        print(tcols.HEADER + "\nTraining done... Testing model." + tcols.ENDC)
        y_pred = tf.nn.softmax(model.predict(self.jet_data.test_data)).numpy()
        accuracy = tf.keras.metrics.CategoricalAccuracy()
        accuracy.update_state(self.jet_data.test_target, y_pred)

        return accuracy.result().numpy()


def get_tensorflow_callbacks():
    """Prepare the callbacks for the training."""
    early_stopping = keras.callbacks.EarlyStopping(
        monitor="val_categorical_accuracy", patience=15
    )
    learning = keras.callbacks.ReduceLROnPlateau(
        monitor="val_categorical_accuracy", factor=0.8, patience=10, min_lr=0.00001
    )

    return [early_stopping, learning]


class OptunaPruner(keras.callbacks.Callback):
    """This is useless since an implementation by the optuna ppl exists already xDD."""

    def __init__(self, trial):
        super(OptunaPruner, self).__init__()
        self.trial = trial

    def on_epoch_end(self, epoch, logs=None):
        self.trial.report(logs["val_categorical_accuracy"], epoch)
        if self.trial.should_prune():
            trial = self.trial
            raise optuna.TrialPruned()
