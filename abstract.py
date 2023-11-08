# -*- coding: utf-8 -*-
"""
Created on Fri Mar 17 07:10:20 2023

@author: jintonic
"""


import datetime as dt
import json
import os
import pickle
import random
import time

import numpy as np
import pandas as pd
import torch
from kaggle.api.kaggle_api_extended import KaggleApi
from sklearn.model_selection import KFold

try:
    from mlutil.features import ABSFeatureGenerator
    from mlutil.mlbase import MLBase
    from mlutil.util import mlflow
    from mlutil.util.notifier import SlackChannel, slack_notify
except ImportError:
    from mymodules.mlutil.features import ABSFeatureGenerator
    from mymodules.mlutil.mlbase import MLBase
    from mymodules.mlutil.util import mlflow
    from mymodules.mlutil.util.notifier import SlackChannel, slack_notify

SEED = 42


def watch_submit_time():
    api = KaggleApi()
    api.authenticate()
    COMPETITION = "predict-student-performance-from-game-play"
    result_ = api.competition_submissions(COMPETITION)[0]
    latest_ref = str(result_)  # 最新のサブミット番号
    submit_time = result_.date
    status = ""

    while status != "complete":
        list_of_submission = api.competition_submissions(COMPETITION)
        for result in list_of_submission:
            if str(result.ref) == latest_ref:
                break
        status = result.status

        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        elapsed_time = int((now - submit_time).seconds / 60) + 1
        if status == "complete":
            print("\r", f"run-time: {elapsed_time} min, LB: {result.publicScore}")
        else:
            print("\r", f"elapsed time: {elapsed_time} min", end="")
            time.sleep(60)


class ABSCallable:
    data_dir = "./data/"

    def __init__(self):
        pass

    def __call__(self, df: pd.DataFrame) -> pd.DataFrame:
        return self.main(df)

    def main(self, df: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError()


class ABSDataFetcher(ABSCallable):
    def __call__(self, dry_run: bool = False) -> pd.DataFrame:
        return self.main(dry_run)

    def main(self, dry_run: bool):
        raise NotImplementedError()


class ABSDataPreprocessor(ABSCallable):
    pass


def init_preprocessor(*args):
    def _apply(df):
        for processor in args:
            df = processor(df)
        return df

    return _apply


class ABSDataPostprocessor(ABSCallable):
    def __init__(self, save_dir=None):
        self.save_dir = save_dir
        if save_dir:
            self._init_dir()

    def main(self, df: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError()

    def save(self, processor):
        path = os.path.join(self.save_dir, self._get_file_name())
        with open(path, "wb") as f:
            pickle.dump(processor, f)

    def load(self, path):
        with open(path, mode="rb") as f:
            return pickle.load(f)

    def _init_dir(self):
        try:
            if not os.path.exists(self.save_dir):
                os.makedirs(self.save_dir)
        # Note: kaggle notebook用
        except OSError:
            pass

    def _get_file_name(self):
        return self.__class__.__name__.lower() + ".pickle"


class ABSDataSplitter:
    def __init__(self, n_splits=5):
        self.n_splits = n_splits

    def train_test_split(self, df: pd.DataFrame) -> tuple:
        raise NotImplementedError()

    def cv_split(self, df: pd.DataFrame) -> tuple:
        """
        Parameters
        ----------
        df : pd.DataFrame

        Yields
        -------
        train: pd.DataFrame, valid: pd.DataFrame
        """
        raise NotImplementedError()

    def k_fold(self, df: pd.DataFrame) -> tuple:
        k_fold = KFold(n_splits=self.n_splits, shuffle=True, random_state=SEED)
        for train_index, valid_index in k_fold.split(df):
            train = df.iloc[train_index]
            valid = df.iloc[valid_index]
            yield train, valid

    def group_k_fold(self, df: pd.DataFrame, group_col: str) -> tuple:
        # Note: sklearn.GroupKFoldeはshuffleとrandom_stateを指定できないため自炊
        k_fold = KFold(n_splits=self.n_splits, shuffle=True, random_state=SEED)
        groups = df[group_col].unique()
        for train_index, valid_index in k_fold.split(groups):
            train = df[df[group_col].isin(groups[train_index])]
            valid = df[df[group_col].isin(groups[valid_index])]
            yield train, valid


class ABSSubmitter:
    competition_name = ""
    experiment_name = ""
    artifact_dir = "/kaggle/input/artifact/"

    def __init__(
        self,
        data_fetcher: ABSDataFetcher,
        data_preprocessor: ABSDataPreprocessor,
        feature_generator: ABSFeatureGenerator,
        data_splitter: ABSDataSplitter,
        #  data_postprocessor: ABSDataPostprocessor,
        model: MLBase,
        submission_comment: str,
    ):
        """
        Parameters
        ----------
        data_fetcher: ABSDataFetcher
        data_preprocessor: ABSDataPreprocessor
        feature_generator: ABSFeatureGenerator
        data_splitter: ABSDataSplitter
        # data_postprocessor: ABSDataPostprocessor
        model: ABSModel, MLBase
        submission_comment: str
            The Message for submission.
        """

        if not self.competition_name:
            raise ValueError("competition_name must be specified.")
        if not self.experiment_name:
            raise ValueError("experiment_name must be specified.")

        self.data_fetcher = data_fetcher
        self.data_preprocessor = data_preprocessor
        self.feature_generator = feature_generator
        self.data_splitter = data_splitter
        # self.data_postprocessor = data_postprocessor
        self.model = model
        self.submission_comment = submission_comment
        self.api = self._init_kaggle_api()

    def get_submit_data(self, test: pd.DataFrame) -> pd.DataFrame:
        sub = self.model.estimate(test)
        if self.model.target_col in sub:
            sub = sub.drop(columns=self.model.target_col)
        sub = sub.rename(columns={"pred": self.model.target_col})
        return sub

    def validate_submit_data(self, sub):
        raise NotImplementedError()

    def get_metrics(self, res):
        """
        分類の場合はoof_metricも利用可
        """
        cv_metrics = res.cv_metrics
        cv_mean = np.array(cv_metrics).mean()
        cv_std = np.array(cv_metrics).std()
        cv_sharpe = self._calc_sharpe(cv_mean, cv_std)
        print(f"CV metrics: {[round(i, 4) for i in cv_metrics]}")
        print(
            f"mean: {round(cv_mean, 4)}, std: {round(cv_std, 4)}, sharpe: {round(cv_sharpe, 4)}"
        )
        return {"cv_mean": cv_mean, "cv_std": cv_std, "cv_sharpe": cv_sharpe}

    def get_experiment_params(self) -> dict:
        return {"model": self.model.model_base_name}

    def make_submission(
        self,
        params_info: dict = {},
        retrain_all_data: bool = False,
        save_model: bool = True,
        dry_run: bool = False,
        return_only: bool = False,
    ):
        data = self.process_data(dry_run=dry_run)
        train, test = self.data_splitter.train_test_split(data)
        del data
        res = self._train_and_evaluate(
            train, retrain_all_data=retrain_all_data, save_model=save_model
        )
        sub = self.get_submit_data(test)
        self.validate_submit_data(sub)

        if not dry_run:
            if return_only:
                return sub, res
            else:
                self._submit(sub)
                time.sleep(15)
                self._save_experiment(
                    res=res,
                    sub=sub,
                    params={**self.get_experiment_params(), **params_info},
                )
        else:
            breakpoint()

    def process_data(self, dry_run: bool):
        if self.feature_generator._load_features():
            print("cached features loaded.")
            data = self.feature_generator.df
        else:
            data = self._process_data(dry_run=dry_run)

        del self.feature_generator.df
        self.model.categorical_columns = self.feature_generator.cat_cols
        return data

    def _process_data(self, dry_run: bool):
        data = self.data_fetcher(dry_run=dry_run)
        data = self.data_preprocessor(data)
        data = self.feature_generator(data)
        # data = self.data_postprocessor(data)
        return data

    def _train_and_evaluate(
        self,
        train: pd.DataFrame,
        retrain_all_data: bool = False,
        save_model: bool = True,
    ) -> list:
        self.model.categorical_columns = self.feature_generator.cat_cols
        fold_generator = self.data_splitter.cv_split(train)
        res = self.model.cv(
            fold_generator, save_model=save_model and not retrain_all_data
        )
        if retrain_all_data:
            self.model.fit(train, save_model=save_model)
        return res

    def _submit(self, sub: pd.DataFrame):
        if not os.path.exists(self.submission_csv_dir):
            os.makedirs(self.submission_csv_dir)
        file_name = f"{self.submission_csv_dir}submission.csv"
        sub.to_csv(file_name, index=False)
        self.api.competition_submit(
            file_name=file_name,
            message=self.submission_comment,
            competition=self.competition_name,
        )

    def _init_kaggle_api(self) -> any:
        # kaggle notebook上で失敗するため
        try:
            from kaggle.api.kaggle_api_extended import KaggleApi

            api = KaggleApi()
            api.authenticate()
            return api
        except OSError:
            return None

    def _get_public_score(self) -> float:
        sub = self.api.competitions_submissions_list(self.competition_name)
        sub = pd.DataFrame(sub)
        sub["date"] = pd.to_datetime(sub["date"])
        score = sub.sort_values("date", ascending=False)["publicScoreNullable"].iloc[0]
        score = float(score) if score else np.nan
        return score

    def _save_experiment(self, res: dict, sub: pd.DataFrame, params: dict):
        public_score = self._get_public_score()
        metrics = self.get_metrics(res)
        metrics["public_score"] = public_score
        mlflow.run(
            experiment_name=self.experiment_name,
            run_name=self.submission_comment,
            params=params,
            metrics=metrics,
            artifact_paths=[self.model.model_dir],
        )
        message = f"experiment finished. metrics:\n{json.dumps(metrics)}"
        slack_notify(message, channel=SlackChannel.regular)
        sub.to_csv(
            os.path.join(
                self.artifact_dir,
                f"submission/{self.submission_comment}/submission.csv",
            ),
            index=False,
        )
        print(f"Public score: {public_score}")

    def _calc_sharpe(self, mean, std):
        return mean / (std + 1)

    def seed_everything(self, seed=None):
        if seed is None:
            seed = SEED
        random.seed(seed)
        os.environ["PYTHONHASHseed"] = str(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)


class CodeSubmitter(ABSSubmitter):
    def estimate(
        self, test: pd.DataFrame, sub: pd.DataFrame, proba: bool
    ) -> pd.DataFrame:
        raise NotImplementedError()

    def get_mock_api(self):
        """
        from public_timeseries_testing_util
        return MockAPI
        """
        raise NotImplementedError()

    def experiment(
        self,
        params_info={},
        retrain_all_data: bool = False,
        dry_run: bool = False,
        return_only: bool = False,
    ):
        df = self.process_data(dry_run=dry_run)
        res = self._train_and_evaluate(features=df, retrain_all_data=retrain_all_data)
        if not dry_run:
            if return_only:
                return res
            else:
                experiment_params = {**self.get_experiment_params(), **params_info}
                self._save_experiment(res, params=experiment_params)
        else:
            breakpoint()

    def load_model(self):
        self.model.load_model()

    def _save_experiment(self, res, params):
        # Note: コードコンペのためsubmitおよびPublic scoreの記録は手動
        metrics = self.get_metrics(res)
        metrics["public_score"] = 0.0
        mlflow.run(
            experiment_name=self.experiment_name,
            run_name=self.submission_comment,
            params=params,
            metrics=metrics,
            artifact_paths=[self.model.model_dir, self.model.oof_dir],
        )
        message = f"experiment finished. metrics:\n{json.dumps(metrics)}"
        slack_notify(message, SlackChannel.regular)
