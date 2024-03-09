import numpy as np
import pandas as pd

from pathlib import Path
from colorama import Fore, Style
from dateutil.parser import parse
from typing import Dict, List, Tuple, Sequence
from datetime import datetime

from power.params import *
from power.ml_ops.data import get_data_with_cache, load_data_to_bq, clean_pv_data
from power.ml_ops.model import initialize_model, compile_model, train_model
from power.ml_ops.registry import load_model, save_model, save_results
from power.ml_ops.cross_val import get_X_y_seq

def preprocess(start_date:str = '1980-01-01',
               stop_date:str = '2022-12-31') -> None:
    """
    - Query the raw dataset from Le Wagon's BigQuery dataset
    - Cache query result as a local CSV if it doesn't exist locally
    - Process query data
    - Store processed data on your personal BQ (truncate existing table if it exists)
    - No need to cache processed data as CSV (it will be cached when queried back from BQ during training)
    """

    print(Fore.MAGENTA + "\n ⭐️ Use case: preprocess" + Style.RESET_ALL)

    # Query raw data from BUCKET BigQuery using `get_data_with_cache`
    query = f"""
        SELECT *
        FROM {GCP_PROJECT}.{BQ_DATASET}.raw_pv
        ORDER BY _0
    """

    # Retrieve data using `get_data_with_cache`
    data_query_cache_path = Path(LOCAL_DATA_PATH).joinpath("raw", f"raw_pv.csv")
    data_query = get_data_with_cache(
        query=query,
        gcp_project=GCP_PROJECT,
        cache_path=data_query_cache_path,
        data_has_header=True
    )

    # Process data
    data_clean = clean_pv_data(data_query)


    load_data_to_bq(
        data_clean,
        gcp_project=GCP_PROJECT,
        bq_dataset=BQ_DATASET,
        table=f'processed_pv',
        truncate=True
    )

    print("✅ preprocess() done \n")



def train(
        start_date:str = '1980-01-01',
        stop_date:str = '2019-12-30',
        split_ratio: float = 0.02, # 0.02 represents ~ 1 month of validation data on a 2009-2015 train set
        learning_rate=0.02,
        batch_size = 32,
        patience = 5
    ) -> float:

    """
    - Download processed data from your BQ table (or from cache if it exists)
    - Train on the preprocessed dataset (which should be ordered by date)
    - Store training results and model weights

    Return val_mae as a float
    """

    print(Fore.MAGENTA + "\n⭐️ Use case: train" + Style.RESET_ALL)
    print(Fore.BLUE + "\nLoading preprocessed validation data..." + Style.RESET_ALL)


    # Load processed data using `get_data_with_cache` in chronological order
    query = f"""
        SELECT *
        FROM {GCP_PROJECT}.{BQ_DATASET}.processed_pv
        ORDER BY utc_time
    """

    data_processed_cache_path = Path(LOCAL_DATA_PATH).joinpath("processed", f"processed_pv.csv")
    data_processed = get_data_with_cache(
        gcp_project=GCP_PROJECT,
        query=query,
        cache_path=data_processed_cache_path,
        data_has_header=True
    )

    # the model uses power as feature -> fix that in raw data
    data_processed = data_processed.rename(columns={'electricity': 'power'})
    # the processed data from bq needs to be converted to datetime object
    data_processed.utc_time = pd.to_datetime(data_processed.utc_time,utc=True)

    if data_processed.shape[0] < 240:
        print("❌ Not enough processed data retrieved to train on")
        return None


    # Split the data into training and testing sets
    train = data_processed[data_processed['utc_time'] < '2020-01-01']
    test = data_processed[data_processed['utc_time'] >= '2020-01-01']

    train = train[['power']]
    test = test[['power']]

    X_train, y_train = get_X_y_seq(train,
                                   number_of_sequences=10_000,
                                   input_length=48,
                                   output_length=24)


    # Train model using `model.py`
    model = load_model()

    if model is None:
        model = initialize_model(X_train, y_train, n_unit=24)

    model = compile_model(model, learning_rate=learning_rate)
    model, history = train_model(model,
                                X_train,
                                y_train,
                                validation_split = 0.3,
                                batch_size = 32,
                                epochs = 50
                                )

    val_mae = np.min(history.history['val_mae'])

    params = dict(
        context="train",
        training_set_size='40 years worth of data',
        row_count=len(X_train),
    )

    # Save results on the hard drive using taxifare.ml_logic.registry
    save_results(params=params, metrics=dict(mae=val_mae))

    # Save model weight on the hard drive (and optionally on GCS too!)
    save_model(model=model)

    print("✅ train() done \n")

    return val_mae


def evaluate(
        min_date:str = '2014-01-01',
        max_date:str = '2015-01-01',
        stage: str = "Production"
    ) -> float:
    """
    Evaluate the performance of the latest production model on processed data
    Return MAE as a float
    """
    print(Fore.MAGENTA + "\n⭐️ Use case: evaluate" + Style.RESET_ALL)

    model = load_model(stage=stage)
    assert model is not None


    # Query your BigQuery processed table and get data_processed using `get_data_with_cache`
    query = f"""
        SELECT *
        FROM {GCP_PROJECT}.{BQ_DATASET}.processed_pv
        ORDER BY utc_time
    """

    data_processed_cache_path = Path(LOCAL_DATA_PATH).joinpath("processed", f"processed_pv.csv")
    data_processed = get_data_with_cache(
        gcp_project=GCP_PROJECT,
        query=query,
        cache_path=data_processed_cache_path,
        data_has_header=True
    )

    if data_processed.shape[0] == 0:
        print("❌ No data to evaluate on")
        return None

    data_processed = data_processed.to_numpy()

    X_new = data_processed[:, :-1]
    y_new = data_processed[:, -1]

    metrics_dict = evaluate_model(model=model, X=X_new, y=y_new)
    mae = metrics_dict["mae"]

    params = dict(
        context="evaluate", # Package behavior
        training_set_size=DATA_SIZE,
        row_count=len(X_new)
    )

    save_results(params=params, metrics=metrics_dict)

    print("✅ evaluate() done \n")

    return mae


def pred(X_pred:str = '2013-05-08 12:00:00') -> np.ndarray:
    """
    Make a prediction using the latest trained model
    """

    print("\n⭐️ Use case: predict")

    # X_pred = datetime.strptime(X_pred, '%Y-%m-%d %H:%M:%S')
    # reference_datetime = datetime.strptime("1980-01-01 00:00:00", '%Y-%m-%d %H:%M:%S')
    # time_difference = X_pred - reference_datetime
    # time_difference_hours = time_difference.total_seconds() / 3600
    # input_date = X_test[time_difference_hours-47: time_difference_hours+1]



    # if X_pred is None:
    #     X_pred = pd.DataFrame(dict(
    #     pickup_datetime=[pd.Timestamp("2013-07-06 17:18:00", tz='UTC')],
    #     pickup_longitude=[-73.950655],
    #     pickup_latitude=[40.783282],
    #     dropoff_longitude=[-73.984365],
    #     dropoff_latitude=[40.769802],
    #     passenger_count=[1],
    # ))

    model = load_model()
    assert model is not None

    # X_processed = preprocess_features(X_pred)
    # y_pred = model.predict(X_processed)

    # print("\n✅ prediction done: ", y_pred, y_pred.shape, "\n")
    print("\n✅ prediction done: \n")
    return model # change it back! to return y_pred
    # return y_pred


if __name__ == '__main__':
    preprocess(min_date='2009-01-01', max_date='2015-01-01')
    train(min_date='2009-01-01', max_date='2015-01-01')
    evaluate(min_date='2009-01-01', max_date='2015-01-01')
    pred()
