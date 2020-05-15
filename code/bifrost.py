import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import torch
from torch.utils.data import DataLoader
import torch.nn as nn
import torch.nn.functional as F
from torch import optim
from attrdict import AttrDict
import time

from code.make_dataset import check_dataframe, split_df, init_data_params, normalize, \
    tabularize_univariate_datetime, TimeDataset, make_future_dataframe
import code.utils as utils


class FlatNet(nn.Module):
    '''
    Linear regression
    '''

    def __init__(self, d_inputs, d_outputs):
        # Perform initialization of the pytorch superclass
        super(FlatNet, self).__init__()
        self.layers = nn.Sequential(
            nn.Linear(d_inputs, d_outputs),
        )

    def forward(self, x):
        return self.layers(x)



class DeepNet(nn.Module):
    '''
    A simple, general purpose, fully connected network
    '''
    def __init__(self, d_inputs, d_outputs, d_hidden=32, num_hidden_layers=0):
        # Perform initialization of the pytorch superclass
        super(DeepNet, self).__init__()
        self.layers = nn.ModuleList()
        for i in range(num_hidden_layers):
            self.layers.append(nn.Linear(d_inputs, d_hidden, bias=True))
            d_inputs = d_hidden
        self.layers.append(nn.Linear(d_inputs, d_outputs, bias=True))

    def forward(self, x):
        '''
        This method defines the network layering and activation functions
        '''
        activation = F.relu
        for i in range(len(self.layers)):
            if i > 0: x = activation(x)
            x = self.layers[i](x)
        return x


class Bifrost:
    def __init__(self, n_lags=1, n_forecasts=1, n_trend=1, num_hidden_layers=0, normalize=True, verbose=False):
        self.name = "ar-net"
        self.verbose = verbose
        self.n_lags = n_lags
        self.n_forecasts = n_forecasts
        self.n_trend = n_trend
        self.normalize = normalize
        self.num_hidden_layers = num_hidden_layers
        self.d_hidden = 4 * (n_lags + n_forecasts + n_trend)
        self.train_config = AttrDict({# TODO allow to be passed in init
            "lr": 1e-3,
            "lr_decay": 0.95,
            "epochs": 50,
            "batch": 16,
            "est_sparsity": 0.1,  # 0 = fully sparse, 1 = not sparse
            "lambda_delay": 10,  # delays start of regularization by lambda_delay epochs
        })
        self.model = DeepNet(
            d_inputs=self.n_lags + self.n_trend,
            d_outputs=self.n_forecasts,
            d_hidden=self.d_hidden,
            num_hidden_layers=self.num_hidden_layers,
        )
        if self.verbose:
            print(self.model)
            # print(self.model.layers[0].weight)
        self.loss_fn = nn.MSELoss()
        self.optimizer = optim.Adam(self.model.parameters(), lr=self.train_config.lr)
        self.scheduler = optim.lr_scheduler.StepLR(self.optimizer, step_size=1, gamma=self.train_config.lr_decay)

        # self.history_dates = None
        self.history = None
        self.data_params = None
        self.results = {}

    def _prep_data(self, df):
        df = check_dataframe(df)

        if self.data_params is None:
            self.data_params = init_data_params(df, normalize=self.normalize)
            if self.verbose: print(self.data_params)
        df = normalize(df, self.data_params)

        # self.history_dates = pd.to_datetime(df['ds']).sort_values()
        self.history = df.copy(deep=True)

        inputs, input_names, targets = tabularize_univariate_datetime(
            df, self.n_lags, self.n_forecasts, self.n_trend, self.verbose
        )
        dataset = TimeDataset(inputs, input_names, targets)

        if self.verbose:
            # plt.plot(df.loc[:100, 'y'])
            plt.plot(df.loc[:100, 'y_scaled'])
            plt.show()
        return dataset

    def _prep_data_predict(self, df=None):
        if df is None:
            df = self.history.copy()
        else:
            if df.shape[0] == 0:
                raise ValueError('Dataframe has no rows.')
            df = check_dataframe(df)
            assert (self.data_params is not None)
            df = normalize(df, self.data_params)

        n_forecasts = 0
        inputs, input_names, targets = tabularize_univariate_datetime(
            df, self.n_lags, n_forecasts, self.n_trend, self.verbose
        )
        dataset = TimeDataset(inputs, input_names, targets)

        return dataset


    def fit(self, df):
        self._train(df)
        return self

    def test(self, df):
        self._evaluate(df)
        return self

    def train_eval(self, df, valid_p=0.2, true_ar=None):
        df_train, df_val = split_df(check_dataframe(df), self.n_lags, self.n_forecasts, valid_p, inputs_overbleed=True, verbose=self.verbose)

        self._train(df_train)

        self._evaluate(df_val, true_ar)

        return self.results

    def _train(self, df):
        if self.history is not None: # Note: self.data_params should also be None
            raise Exception('Model object can only be fit once. '
                            'Instantiate a new object.')
        assert (self.data_params is None)

        dataset = self._prep_data(df)
        loader = DataLoader(dataset, batch_size=self.train_config["batch"], shuffle=True)

        total_batches = 0
        epoch_losses = []
        epoch_regs = []
        start = time.time()
        for e in range(self.train_config.epochs):
            epoch_loss, epoch_reg, batches = self._train_epoch(e, loader)
            epoch_losses.append(epoch_loss)
            epoch_regs.append(epoch_reg)
            total_batches += batches

        duration = time.time() - start
        self.results["epoch_losses"] = epoch_losses
        self.results["epoch_regularizations"] = epoch_regs
        self.results["loss_train"] = epoch_losses[-1]
        self.results["time_train"] = duration
        if self.verbose:
            print("Train Time: {:8.4f}".format(duration))
            print("Total Number of Batches: ", total_batches)

    def _train_epoch(self, e, loader):
        # slowly increase regularization until lambda_delay epoch
        reg_lambda = utils.get_regularization_lambda(self.train_config.est_sparsity, self.train_config.lambda_delay, e)
        num_batches = 0
        current_epoch_losses = []
        current_epoch_reg_losses = []
        for inputs, targets in loader:
            # Run forward calculation
            predicted = self.model.forward(inputs)

            # Compute loss.
            loss = self.loss_fn(predicted, targets)
            current_epoch_losses.append(loss.data.item())

            # Add regularization
            reg_loss = torch.zeros(1, dtype=torch.float, requires_grad=True)
            if reg_lambda is not None:
                # Warning: will grab first layer as the weight to be regularized!
                abs_weights = torch.abs(self.model.layers[0].weight)
                reg = torch.div(2.0, 1.0 + torch.exp(-3.0 * abs_weights.pow(1.0 / 3.0))) - 1.0
                reg_loss = reg_lambda * (reg_loss + torch.mean(reg))
                loss = loss + reg_loss
            current_epoch_reg_losses.append(reg_loss.data.item())

            self.optimizer.zero_grad()
            loss.backward(retain_graph=True)
            self.optimizer.step()
            num_batches += 1

        self.scheduler.step()
        epoch_loss = np.mean(current_epoch_losses)
        epoch_reg = np.mean(current_epoch_reg_losses)
        if self.verbose:
            print("{}. Epoch Avg Loss: {:10.2f}".format(e + 1, epoch_loss))
        return epoch_loss, epoch_reg, num_batches

    def _evaluate(self, df, true_ar=None):
        if self.history is None:
            raise Exception('Model object needs to be fit first.')
        assert (self.data_params is not None)

        dataset = self._prep_data(df)
        loader = DataLoader(dataset, batch_size=len(dataset), shuffle=False)

        losses = list()
        targets_vectors = list()
        predicted_vectors = list()
        batch_index = 0
        for inputs, targets in loader:
            predicted = self.model.forward(inputs)
            loss = self.loss_fn(predicted, targets)
            losses.append(loss.data.numpy())
            targets_vectors.append(targets.data.numpy())
            predicted_vectors.append(predicted.data.numpy())
            batch_index += 1

        self.results["loss_val"] = np.mean(np.array(losses))
        predicted = np.concatenate(predicted_vectors)
        actual = np.concatenate(targets_vectors)
        # self.results["predicted"] = predicted
        # self.results["actual"] = actual
        mse = np.mean((predicted - actual) ** 2)
        self.results["mse_val"] = mse

        weights_rereversed = self.model.layers[0].weight.detach().numpy()[0, ::-1]
        self.results["weights"] = weights_rereversed

        if true_ar is not None:
            self.results["true_ar"] = true_ar
            sTPE = utils.symmetric_total_percentage_error(self.results["true_ar"], self.results["weights"])
            self.results["sTPE (weights)"] = sTPE
            if self.verbose:
                print("sTPE (weights): {:6.3f}".format(stats["sTPE (weights)"]))
                print("AR params: ")
                print(self.results["true_ar"])
                print("Weights: ")
                print(self.results["weights"])

        if self.verbose:
            print("Validation MSEs: {:10.2f}".format(self.results["mse_val"]))

    def predict(self, df=None):
        # predicts the next n_forecast steps from the last step in the history
        if self.history is None:
            raise Exception('Model has not been fit.')

        dataset = self._prep_data_predict(df)
        inputs, targets = dataset[-1]
        predicted = self.model.forward(inputs)
        # TODO: un-normalize and plot with inputs and forecasts marked.

        # TODO decompose components for components plots

        # TODO: adapt from Prophet
        # df['trend'] = self.predict_trend(df)
        # seasonal_components = self.predict_seasonal_components(df)
        # if self.uncertainty_samples:
        #     intervals = self.predict_uncertainty(df)
        # else:
        #     intervals = None
        #
        # # Drop columns except ds, cap, floor, and trend
        # cols = ['ds', 'trend']
        # if 'cap' in df:
        #     cols.append('cap')
        # if self.logistic_floor:
        #     cols.append('floor')
        # # Add in forecast components
        # df2 = pd.concat((df[cols], intervals, seasonal_components), axis=1)
        # df2['yhat'] = (
        #     df2['trend'] * (1 + df2['multiplicative_terms'])
        #     + df2['additive_terms']
        # )
        # return df2


    def __make_future_dataframe(self, periods, freq='D', include_history=True):
        # This only makes sense if no AR is performed. We will instead go another route
        # We will only predict the next n_forecast steps from the last step in the history
        # directly use self.predict()
        history_dates = pd.to_datetime(self.history['ds']).sort_values()
        return make_future_dataframe(history_dates, periods, freq, include_history)


def test_1():
    df = pd.read_csv('../data/example_air_passengers.csv')
    df.head()
    # print(df.shape)
    seasonality = 12
    train_frac = 0.8
    train_num = int((train_frac * df.shape[0]) // seasonality * seasonality)
    # print(train_num)
    df_train = df.copy(deep=True).iloc[:train_num]
    df_val = df.copy(deep=True).iloc[train_num:]

    m = Bifrost(
        n_lags=seasonality,
        n_forecasts=1,
        n_trend=1,
        num_hidden_layers=0,
        normalize=True,
        verbose=False,
    )

    m = m.fit(df_train)

    m = m.test(df_val)
    for stat, value in m.results.items():
        print(stat, value)

def test_2():
    df = pd.read_csv('../data/example_wp_log_peyton_manning.csv')
    m = Bifrost(verbose=True)
    m.fit(df)
    future = m.make_future_dataframe(periods=12)
    print(future.tail())
    # forecast = m.predict(future)
    # fig1 = m.plot(forecast)
    # fig2 = m.plot_components(forecast)

def main():
    # test_1()
    test_2()




if __name__ == '__main__':
    main()