import datetime
import optuna
import pandas as pd
import sklearn
import numpy as np
import os
import glob
import shutil
from sklearn.model_selection import train_test_split
import csv
import time
import traceback
import copy
import configparser

from ..preprocess import base_dataset
from ..utils import helper_functions
from ..evaluation import eval_metrics
from ..model import _base_model, _model_functions, _torch_model


class OptunaOptim:
    """
    Class that contains all info for the whole optimization using optuna for one model and dataset

    ** Attributes **

        - current_model_name (*str*): name of the current model according to naming of .py alkle in package model
        - dataset (*obj:`~ForeTiS.preprocess.base_dataset.Dataset*): dataset to use for optimization run
        - base_path (*str*): base_path for save_path
        - save_path (*str*): path for model and results storing
        - study (*optuna.study.Study*): optuna study for optimization run
        - current_best_val_result (*float*): the best validation result so far
        - early_stopping_point (*int*): point at which early stopping occured (relevant for some models)
        - target_column (*str*): target column for which predictions shall be made
        - user_input_params (*dict*): all params handed over to the constructor that are needed in the whole class

    :param save_dir: directory for saving the results.
    :param test_set_size_percentage: size of the test set relevant for cv-test and train-val-test
    :param val_set_size_percentage: size of the validation set relevant for train-val-test
    :param n_trials: number of trials for optuna
    :param save_final_model: specify if the final model should be saved
    :param batch_size: batch size for neural network models
    :param n_epochs: number of epochs for neural network models
    :param current_model_name: name of the current model according to naming of .py file in package model
    :param periodical_refit_cycles: if and for which intervals periodical refitting should be performed
    :param refit_drops: after how many periods the model should get updated
    :param target_column: target column for which predictions shall be made
    :param intermediate_results_interval: number of trials after which intermediate results will be saved
    """

    def __init__(self, save_dir: str, data: str, featureset: str, datasplit: str, test_set_size_percentage: int,
                 val_set_size_percentage: int, n_splits: int, models: list, n_trials: int, save_final_model: bool,
                 batch_size: int, n_epochs: int, num_monte_carlo: int, current_model_name: str,
                 datasets: base_dataset.Dataset, periodical_refit_cycles: list, refit_drops: int, refit_window: int,
                 target_column: str, intermediate_results_interval: int = 50, config: configparser.ConfigParser = None):
        self.current_model_name = current_model_name
        self.datasets = datasets
        self.base_path = save_dir + '/results/' + current_model_name + '/' + \
                         datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + '/'
        if not os.path.exists(self.base_path):
            os.makedirs(self.base_path)
        self.featureset = featureset
        self.save_path = self.base_path
        self.study = None
        self.current_best_val_result = None
        self.early_stopping_point = None
        self.target_column = target_column
        self.test_set_size_percentage = test_set_size_percentage
        self.n_splits = n_splits
        self.datasplit = datasplit
        self.seasonal_periods = config[data].getint('seasonal_periods')
        self.user_input_params = locals()  # distribute all handed over params in whole class

    def create_new_study(self) -> optuna.study.Study:
        """
        Method to create a new optuna study
        :return: optuna study
        """
        study_name = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + '_' + '-MODEL' + self.current_model_name\
                     + '-TRIALS' + str(self.user_input_params["n_trials"])
        storage = optuna.storages.RDBStorage(
            "sqlite:///" + self.save_path + 'Optuna_DB-' + study_name + ".db", heartbeat_interval=60, grace_period=120,
            failed_trial_callback=optuna.storages.RetryFailedTrialCallback(max_retry=3)
        )
        study = optuna.create_study(
            storage=storage, study_name=study_name, direction='minimize', load_if_exists=True,
            sampler=optuna.samplers.TPESampler(seed=42),
            pruner=optuna.pruners.PercentilePruner(percentile=80, n_min_trials=20)
        )

        return study

    def objective(self, trial: optuna.trial.Trial):
        """
        Objective function for optuna optimization that returns a score
        :param trial: trial of optuna for optimization
        :return: score of the current hyperparameter config
        """
        if (trial.number != 0) and (self.user_input_params["intermediate_results_interval"] is not None) and (
                trial.number % self.user_input_params["intermediate_results_interval"] == 0):
            print('Generate intermediate test results at trial ' + str(trial.number))
            _ = self.generate_results_on_test()
        # Create model
        # Setup timers for runtime logging
        start_process_time = time.process_time()
        start_realclock_time = time.time()
        # in case a model has attributes not part of the base class hand them over in a dictionary to keep the same call
        # (name of the attribute and key in the dictionary have to match)
        additional_attributes_dict = {}
        if issubclass(helper_functions.get_mapping_name_to_class()[self.current_model_name], _torch_model.TorchModel):
            # additional attributes for torch models
            additional_attributes_dict['batch_size'] = self.user_input_params["batch_size"]
            additional_attributes_dict['n_epochs'] = self.user_input_params["n_epochs"]
            additional_attributes_dict['num_monte_carlo'] = self.user_input_params["num_monte_carlo"]
            early_stopping_points = []  # log early stopping point at each fold for torch and tensorflow models
        try:
            model: _base_model.BaseModel = helper_functions.get_mapping_name_to_class()[self.current_model_name](
                optuna_trial=trial, datasets=self.datasets,  featureset=self.featureset,
                test_set_size_percentage=self.test_set_size_percentage, target_column=self.target_column,
                current_model_name=self.current_model_name,
                **additional_attributes_dict
            )
        except Exception as exc:
            print(traceback.format_exc())
            print(exc)
            print(trial.params)
            print('Trial failed. Error in model creation.')
            self.clean_up_after_exception(trial_number=trial.number, trial_params=trial.params,
                                          reason='model creation: ' + str(exc))
            raise optuna.exceptions.TrialPruned()

        self.dataset = model.dataset
        # some model can not procduce reliable forecasts with less than two seasonal cycles in the data, what can
        # happen with timeseries-cv. In this case, datasplit will be set to train-val-test
        if self.test_set_size_percentage == 2021:
            test = self.dataset.loc['2021-01-01': '2021-12-31']
            train_val = pd.concat([self.dataset, test]).drop_duplicates(keep=False)
            train_val.index.freq = train_val.index.inferred_freq
            smallest_split_length = train_val.shape[0] / self.n_splits
        else:
            smallest_split_length = (100 - self.test_set_size_percentage) * 0.01 * self.dataset.shape[0] / self.n_splits
        if self.datasplit == 'cv' and self.current_model_name == 'es':
            print('Exponential Smoothing depends on continuous time series. Will set datasplit to timeseries-cv.')
            self.datasplit = 'timeseries-cv'
        elif self.datasplit == 'timeseries-cv' and smallest_split_length < 2 * self.datasets.seasonal_periods:
            print('First timeseries-cv split has less than 2 seasonal cycles. Will set datasplit to train-val-test.')
            self.datasplit = 'train-val-test'

        # save the unfitted model
        os.makedirs(self.save_path + 'temp/', exist_ok=True)
        model.save_model(path=self.save_path + 'temp/',
                         filename='unfitted_model_trial' + str(trial.number))
        print('Params for Trial ' + str(trial.number))
        print(trial.params)
        if self.check_params_for_duplicate(current_params=trial.params):
            print('Trial params are a duplicate.')
            self.clean_up_after_exception(trial_number=trial.number, trial_params=trial.params,
                                          reason='pruned: duplicate')
            raise optuna.exceptions.TrialPruned()
        # Iterate over all folds
        objective_values = []
        validation_results = pd.DataFrame(index=range(0, self.dataset.shape[0]))

        folds = helper_functions.get_folds(datasplit=self.datasplit,
                                           n_splits=self.user_input_params["n_splits"])

        for fold in range(folds):
            fold_name = "fold_" + str(fold)
            if self.test_set_size_percentage == 2021:
                pass
            else:
                train_val, _ = train_test_split(self.dataset,
                                                   test_size=self.user_input_params["test_set_size_percentage"] * 0.01,
                                                   random_state=42, shuffle=False)
            if self.datasplit == "timeseries-cv" or self.datasplit == "cv":
                train_indexes, val_indexes = helper_functions.get_indexes(df=train_val,
                                                                          n_splits=self.user_input_params["n_splits"],
                                                                          datasplit=self.datasplit)
                train, val = train_val.iloc[train_indexes[fold]], train_val.iloc[val_indexes[fold]]
            else:
                train, val = train_test_split(train_val,
                                              test_size=self.user_input_params["val_set_size_percentage"] * 0.01,
                                              random_state=42, shuffle=False)

            # load the unfitted model to prevent information leak between folds
            model = _model_functions.load_model(path=self.save_path + 'temp/',
                                                filename='unfitted_model_trial' + str(trial.number))

            try:
                # run train and validation loop for this fold
                try:
                    y_pred = model.train_val_loop(train=train, val=val)[0]
                except Exception as exc:
                    print('Trial failed. Error in model training.')
                    print(traceback.format_exc())
                    print(exc)
                    print(trial.params)
                    self.clean_up_after_exception(trial_number=trial.number, trial_params=trial.params,
                                                  reason='model creation: ' + str(exc))
                    raise optuna.exceptions.TrialPruned()

                if hasattr(model, 'early_stopping_point'):
                    early_stopping_points.append(
                        model.early_stopping_point if model.early_stopping_point is not None else model.n_epochs)

                if len(y_pred) == (len(val) - 1):
                    # might happen if batch size leads to a last batch with one sample which will be dropped then
                    print('val has one element less than y_true (e.g. due to batch size) -> drop last element')
                    val = val[:-1]

                objective_value = sklearn.metrics.mean_squared_error(y_true=val[self.target_column], y_pred=y_pred)
                # report value for pruning
                trial.report(value=objective_value,
                             step=0 if self.datasplit == 'train-val-test' else int(fold_name[-1]))
                if trial.should_prune():
                    self.clean_up_after_exception(trial_number=trial.number, trial_params=trial.params,
                                                  reason='pruned')
                    raise optuna.exceptions.TrialPruned()

                # store results
                objective_values.append(objective_value)
                validation_results.at[0:len(train) - 1, fold_name + '_train_true'] = train[self.target_column]

                if 'lstm' in self.current_model_name:
                    try:
                        validation_results.at[0:len(train) - model.seq_length - 1, fold_name + '_train_pred'] = \
                            model.predict(X_in=train)[0].flatten()
                    except:
                        validation_results.at[0:len(train) - model.seq_length - 2, fold_name + '_train_pred'] = \
                            model.predict(X_in=train)[0].flatten()
                else:
                    validation_results.at[0:len(train) - 1, fold_name + '_train_pred'] = \
                        model.predict(X_in=train)[0].flatten()

                validation_results.at[0:len(val) - 1, fold_name + '_val_true'] = \
                    val.loc[:, [self.target_column]].values.reshape(-1)
                validation_results.at[0:len(y_pred) - 1, fold_name + '_val_pred'] = y_pred.flatten()

                for metric, value in eval_metrics.get_evaluation_report(y_pred=y_pred, y_true=val[self.target_column],
                                                                        prefix=fold_name + '_').items():
                    validation_results.at[0, metric] = value
            except (RuntimeError, TypeError, ValueError, np.linalg.LinAlgError) as exc:
                print(traceback.format_exc())
                print(exc)
                print('Trial failed. Error in optim loop.')
                self.clean_up_after_exception(trial_number=trial.number, trial_params=trial.params,
                                              reason='model optimization: ' + str(exc))
                raise optuna.exceptions.TrialPruned()
        current_val_result = float(np.mean(objective_values))
        if self.current_best_val_result is None or current_val_result < self.current_best_val_result:
            if hasattr(model, 'early_stopping_point'):
                # take mean of early stopping points of all folds for refitting of final model
                self.early_stopping_point = int(np.mean(early_stopping_points))
            self.current_best_val_result = current_val_result
            # persist results
            validation_results.to_csv(self.save_path + 'temp/validation_results_trial' + str(trial.number) + '.csv',
                                      sep=',', decimal='.', float_format='%.10f', index=False)
            # delete previous results
            for file in os.listdir(self.save_path + 'temp/'):
                if 'trial' + str(trial.number) not in file:
                    os.remove(self.save_path + 'temp/' + file)
        else:
            # delete unfitted model
            os.remove(self.save_path + 'temp/' + 'unfitted_model_trial' + str(trial.number))

        # save runtime information of this trial
        self.write_runtime_csv(dict_runtime={'Trial': trial.number,
                                             'process_time_s': time.process_time() - start_process_time,
                                             'real_time_s': time.time() - start_realclock_time,
                                             'params': trial.params, 'note': 'successful'})

        return current_val_result

    def clean_up_after_exception(self, trial_number: int, trial_params: dict, reason: str):
        """
        Clean up things after an exception: delete unfitted model if it exists and update runtime csv
        :param trial_number: number of the trial
        :param trial_params: parameters of the trial
        :param reason: hint for the reason of the Exception
        """
        if os.path.exists(self.save_path + 'temp/' + 'unfitted_model_trial' + str(trial_number)):
            os.remove(self.save_path + 'temp/' + 'unfitted_model_trial' + str(trial_number))
        self.write_runtime_csv(dict_runtime={'Trial': trial_number, 'process_time_s': np.nan, 'real_time_s': np.nan,
                                             'params': trial_params, 'note': reason})

    def write_runtime_csv(self, dict_runtime: dict):
        """
        Write runtime info to runtime csv file
        :param dict_runtime: dictionary with runtime information
        """
        with open(self.save_path + self.current_model_name + '_runtime_overview.csv', 'a') as runtime_file:
            headers = ['Trial', 'refitting_cycle', 'process_time_s', 'real_time_s', 'params', 'note']
            writer = csv.DictWriter(f=runtime_file, fieldnames=headers)
            if runtime_file.tell() == 0:
                writer.writeheader()
            writer.writerow(dict_runtime)

    def calc_runtime_stats(self) -> dict:
        """
        Calculate runtime stats for saved csv file.
        :return: dict with runtime info enhanced with runtime stats
        """
        csv_file = pd.read_csv(self.save_path + self.current_model_name + '_runtime_overview.csv')
        if csv_file['Trial'].dtype is object and any(["retrain" in elem for elem in csv_file["Trial"]]):
            csv_file = csv_file[csv_file["Trial"].str.contains("retrain") is False]
        process_times = csv_file['process_time_s']
        real_times = csv_file['real_time_s']
        process_time_mean, process_time_std, process_time_max, process_time_min = \
            process_times.mean(), process_times.std(), process_times.max(), process_times.min()
        real_time_mean, real_time_std, real_time_max, real_time_min = \
            real_times.mean(), real_times.std(), real_times.max(), real_times.min()
        self.write_runtime_csv({'Trial': 'mean', 'process_time_s': process_time_mean, 'real_time_s': real_time_mean})
        self.write_runtime_csv({'Trial': 'std', 'process_time_s': process_time_std, 'real_time_s': real_time_std})
        self.write_runtime_csv({'Trial': 'max', 'process_time_s': process_time_max, 'real_time_s': real_time_max})
        self.write_runtime_csv({'Trial': 'min', 'process_time_s': process_time_min, 'real_time_s': real_time_min})
        return {'process_time_mean': process_time_mean, 'process_time_std': process_time_std,
                'process_time_max': process_time_max, 'process_time_min': process_time_min,
                'real_time_mean': real_time_mean, 'real_time_std': real_time_std,
                'real_time_max': real_time_max, 'real_time_min': real_time_min}

    def check_params_for_duplicate(self, current_params: dict) -> bool:
        """
        Check if params were already suggested which might happen by design of TPE sampler.
        :param current_params: dictionary with current parameters
        :return: bool reflecting if current params were already used in the same study
        """
        past_params = [trial.params for trial in self.study.trials[:-1]]
        return current_params in past_params

    def generate_results_on_test(self) -> dict:
        """
        Calculate final evaluation scores.
        :return: final evaluation scores
        """
        helper_functions.set_all_seeds()
        print("## Retrain best model and test ##")
        # Retrain on full train + val data with best hyperparams and apply on test
        prefix = '' if len(self.study.trials) == self.user_input_params["n_trials"] else '/temp/'
        if self.test_set_size_percentage == 2021:
            test = self.dataset.loc['2020-01-01': '2020-12-31']
            retrain = pd.concat([self.dataset, test]).drop_duplicates(keep=False)
        else:
            retrain, test = train_test_split(self.dataset,
                                             test_size=self.user_input_params["test_set_size_percentage"] * 0.01,
                                             random_state=42, shuffle=False)
        start_process_time = time.process_time()
        start_realclock_time = time.time()
        final_model = _model_functions.load_retrain_model(
            path=self.save_path, filename=prefix + 'unfitted_model_trial' + str(self.study.best_trial.number),
            retrain=retrain, early_stopping_point=self.early_stopping_point)
        if len(self.study.trials) == self.user_input_params["n_trials"] and self.user_input_params["save_final_model"]:
            final_model.save_model(path=self.save_path, filename='final_retrained_model')
        y_pred_retrain = final_model.predict(X_in=retrain)[0]
        final_model.var_artifical = np.quantile(
            retrain[final_model.target_column][-len(final_model.prediction):] - y_pred_retrain, 0.68) ** 2
        final_results = pd.DataFrame(index=range(0, self.dataset.shape[0]))
        final_results.at[0:len(y_pred_retrain) - 1, 'y_pred_retrain'] = y_pred_retrain.flatten()
        final_results.at[0:len(retrain) - 1, 'y_true_retrain'] = retrain[self.target_column].values.flatten()
        final_results.at[0:len(test) - 1, 'y_true_test'] = test[self.target_column].values.flatten()
        feature_importance = pd.DataFrame(index=range(0, 0))
        for count, period in enumerate(self.user_input_params["periodical_refit_cycles"]):
            test_len = test.shape[0]
            if hasattr(final_model, 'sequential'):
                test = self.dataset.tail(len(test) + final_model.seq_length)
            model = copy.deepcopy(final_model)
            if period == 'complete':
                if hasattr(final_model, 'variance'):
                    y_pred_test, y_pred_test_var_artifical, y_pred_test_var = model.predict(X_in=test)
                else:
                    y_pred_test, y_pred_test_var_artifical = model.predict(X_in=test)
                y_pred_test_var_artifical= np.full((len(y_pred_test),), y_pred_test_var_artifical)
            elif period == 0:
                model.retrain(retrain=retrain.tail(self.user_input_params['refit_window']*self.seasonal_periods))
                if hasattr(final_model, 'variance'):
                    y_pred_test, y_pred_test_var_artifical, y_pred_test_var = model.predict(X_in=test)
                else:
                    y_pred_test, y_pred_test_var_artifical = model.predict(X_in=test)
                y_pred_test_var_artifical = np.full((len(y_pred_test),), y_pred_test_var_artifical)
            else:
                y_pred_test = list()
                y_pred_test_var_artifical = list()
                if hasattr(final_model, 'variance'):
                    y_pred_test_var = list()
                X_train_val_manip = retrain.tail(self.user_input_params['refit_window']*self.seasonal_periods).copy()
                if hasattr(model, 'sequential'):
                    x_test = model.X_scaler.transform(test.drop(labels=[self.target_column], axis=1))
                    y_test = model.y_scaler.transform(test[self.target_column].values.reshape(-1, 1))
                    x_test, _ = model.create_sequences(x_test, y_test)
                for i in range(0, test_len):
                    if hasattr(model, 'sequential'):
                        if hasattr(final_model, 'variance'):
                            y_pred_test_pred, y_pred_test_pred_var_artifical, y_pred_test_pred_var \
                                = model.predict(X_in=x_test[i])
                            y_pred_test_var.append(y_pred_test_pred_var)
                        else:
                            y_pred_test_pred, y_pred_test_pred_var_artifical = model.predict(X_in=x_test[i])
                        y_pred_test.append(y_pred_test_pred)
                        y_pred_test_var_artifical.append(y_pred_test_pred_var_artifical)
                    else:
                        if hasattr(final_model, 'variance'):
                            y_pred_test_pred, y_pred_test_pred_var_artifical, y_pred_test_pred_var \
                                = model.predict(X_in=test.iloc[[i]])
                            y_pred_test_var.extend(y_pred_test_pred_var)
                        else:
                            y_pred_test_pred, y_pred_test_pred_var_artifical = model.predict(X_in=test.iloc[[i]])
                        y_pred_test.append(y_pred_test_pred)
                        y_pred_test_var_artifical.append(y_pred_test_pred_var_artifical)

                    if (i+1) % period == 0:
                        X_train_val_manip = pd.concat([X_train_val_manip[self.user_input_params["refit_drops"]:],
                                                       test[i+1 - period:i+1]])
                        model.update(update=X_train_val_manip, period=period)
                y_pred_test = np.array(y_pred_test).flatten()
                y_pred_test_var_artifical = np.array(y_pred_test_var_artifical).flatten()
                if hasattr(final_model, 'variance'):
                    if np.array(y_pred_test_var).ndim == 2:
                        y_pred_test_var = np.reshape(np.array(y_pred_test_var), (-1, 2))
                    else:
                        y_pred_test_var = np.array(y_pred_test_var).flatten()

            no_trials = len(self.study.trials) - 1 \
                if (self.user_input_params["intermediate_results_interval"] is not None) and \
                   (len(self.study.trials) % self.user_input_params["intermediate_results_interval"] != 0) \
                else len(self.study.trials)
            self.write_runtime_csv(dict_runtime={'Trial': 'retraining_after_' + str(no_trials) + '_trials',
                                                 'refitting_cycle': period,
                                                 'process_time_s': time.process_time() - start_process_time,
                                                 'real_time_s': time.time() - start_realclock_time,
                                                 'params': self.study.best_trial.params, 'note': 'successful'})

            # Evaluate and save results
            if 'lstm' in self.current_model_name:
                test = self.dataset.tail(test_len)
            eval_scores = eval_metrics.get_evaluation_report(y_true=test[self.target_column], y_pred=y_pred_test,
                                                             prefix='test_refitting_period_' + str(period) + '_',
                                                             current_model_name=self.current_model_name)

            feat_import_df = None
            if self.current_model_name in ['ard', 'bayesridge', 'elasticnet', 'lasso', 'ridge', 'xgboost']:
                feat_import_df = self.get_feature_importance(model=model, period=period)
            feature_importance = pd.concat([feature_importance, feat_import_df], axis=1)

            print('## Results on test set with refitting period: ' + str(period) + ' ##')
            print(eval_scores)
            final_results.at[0:len(y_pred_test) - 1, 'y_pred_test_refitting_period_' + str(period)] = \
                y_pred_test.flatten()
            final_results.at[0:len(y_pred_test_var_artifical) - 1, 'y_pred_test_var_artifical_refitting_period_' +
                                                                   str(period)] = y_pred_test_var_artifical.flatten()
            if hasattr(final_model, 'variance'):
                if y_pred_test_var.ndim == 2:
                    final_results.at[0:len(y_pred_test_var) - 1, 'y_pred_test_lower_bound_refitting_period_'
                                                                 + str(period)] = y_pred_test_var[:, 0].flatten()
                    final_results.at[0:len(y_pred_test_var) - 1, 'y_pred_test_upper_bound_refitting_period_'
                                                                 + str(period)] = y_pred_test_var[:, 1].flatten()
                else:
                    final_results.at[0:len(y_pred_test_var) - 1, 'y_pred_test_var_refitting_period_' +
                                                                 str(period)] = y_pred_test_var.flatten()

            for metric, value in eval_scores.items():
                final_results.at[0, metric] = value
            if count == 0:
                final_eval_scores = eval_scores
            else:
                final_eval_scores = {**final_eval_scores, **eval_scores}

        if len(self.study.trials) == self.user_input_params["n_trials"]:
            results_filename = 'final_model_test_results.csv'
            feat_import_filename = 'final_model_feature_importances.csv'
        else:
            results_filename = '/temp/intermediate_after_' + str(len(self.study.trials) - 1) + '_test_results.csv'
            feat_import_filename = \
                '/temp/intermediate_after_' + str(len(self.study.trials) - 1) + '_feat_importances.csv'
            shutil.copyfile(self.save_path + self.current_model_name + '_runtime_overview.csv',
                            self.save_path + '/temp/intermediate_after_' + str(len(self.study.trials) - 1) + '_' +
                            self.current_model_name + '_runtime_overview.csv', )
        final_results.to_csv(self.save_path + results_filename,
                             sep=',', decimal='.', float_format='%.10f', index=False)
        if feature_importance is not None:
            feature_importance.to_csv(
                self.save_path + feat_import_filename, sep=',', decimal='.', float_format='%.10f', index=False)

        return final_eval_scores

    def get_feature_importance(self, model: _base_model.BaseModel, period: int) -> pd.DataFrame:
        """
        Get feature importances for models that possess such a feature, e.g. XGBoost
        :param model: model to analyze
        :param period: refitting period
        :return: DataFrame with feature importance information
        """
        feat_import_df = pd.DataFrame()
        if self.current_model_name in ['xgboost']:
            feature_importances = model.model.feature_importances_
            sorted_idx = feature_importances.argsort()[::-1]
            feat_import_df['feature_period_' + str(period)] = self.dataset.columns[sorted_idx]
            feat_import_df['feature_importance'] = feature_importances[sorted_idx]
        else:
            coef = model.model.coef_.flatten()
            sorted_idx = coef.argsort()[::-1]
            feat_import_df['feature_period_' + str(period)] = self.dataset.columns[sorted_idx]
            feat_import_df['coefficients'] = coef[sorted_idx]

        return feat_import_df

    @property
    def run_optuna_optimization(self) -> dict:
        """
        Function to run whole optuna optimization for one model, dataset and datasplit
        :return: overall results
        """
        helper_functions.set_all_seeds()
        overall_results = {}
        print("## Starting Optimization")
        self.save_path = self.base_path + "/"
        if not os.path.exists(self.save_path):
            os.makedirs(self.save_path)
        # Create a new study
        self.study = self.create_new_study()
        self.current_best_val_result = None
        # Start optimization run
        self.study.optimize(
            lambda trial: self.objective(trial=trial),
            n_trials=self.user_input_params["n_trials"]
        )
        helper_functions.set_all_seeds()
        # Calculate runtime metrics after finishing optimization
        runtime_metrics = self.calc_runtime_stats()
        # Print statistics after run
        print("## Optuna Study finished ##")
        print("Study statistics: ")
        print("  Finished trials: ", len(self.study.trials))
        print("  Pruned trials: ", len(self.study.get_trials(states=(optuna.trial.TrialState.PRUNED,))))
        print("  Completed trials: ", len(self.study.get_trials(states=(optuna.trial.TrialState.COMPLETE,))))
        print("  Best Trial: ", self.study.best_trial.number)
        print("  Value: ", self.study.best_trial.value)
        print("  Params: ")
        for key, value in self.study.best_trial.params.items():
            print("    {}: {}".format(key, value))

        # Move validation results and models of best trial
        files_to_keep = glob.glob(self.save_path + 'temp/' + '*trial' + str(self.study.best_trial.number) + '*')
        for file in files_to_keep:
            shutil.copyfile(file, self.save_path + file.split('/')[-1])
        shutil.rmtree(self.save_path + 'temp/')

        # Retrain on full train + val data with best hyperparams and apply on test
        final_eval_scores = self.generate_results_on_test()
        overall_results['Test'] = {'best_params': self.study.best_trial.params, 'eval_metrics': final_eval_scores,
                                   'runtime_metrics': runtime_metrics}

        return overall_results
