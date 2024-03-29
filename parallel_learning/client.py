from axon import discovery, client
from common import TwoNN, set_parameters, get_accuracy
from keras.datasets import mnist
import asyncio, torch

num_global_cycles = 10
nb_ip = '192.168.2.19'
BATCH_SIZE = 32

device = 'cpu'
if torch.cuda.is_available(): device = 'cuda:0'

# importing data
raw_data = mnist.load_data()

x_train_raw = raw_data[0][0]
y_train_raw = raw_data[0][1]
x_test_raw = raw_data[1][0]
y_test_raw = raw_data[1][1]

# formatting data
x_train = torch.tensor(x_train_raw, dtype=torch.float32).reshape([-1, 784])
x_test = torch.tensor(x_test_raw, dtype=torch.float32).reshape([-1, 784])

y_train = torch.tensor(y_train_raw, dtype=torch.long)
y_test = torch.tensor(y_test_raw, dtype=torch.long)

# defines the central model, as well as the criterion
net = TwoNN().to(device)
criterion = torch.nn.CrossEntropyLoss()

# this function aggregates parameters from workers
def aggregate_parameters(param_list, weights):

	num_clients = len(param_list)
	avg_params = []

	for i, params in enumerate(param_list):

		if (i == 0):
			for p in params:
				avg_params.append(p.clone()*weights[i])

		else:
			for j, p in enumerate(params):
				avg_params[j].data += p.data*weights[i]

	return avg_params

# gets the accuracy and loss of a neural net on testing data
def val_evaluation(net, x_test, y_test):

	num_test_batches = x_test.shape[0]//BATCH_SIZE

	loss = 0
	acc = 0

	net = net.to(device)

	for batch_number in range(num_test_batches):
		x_batch = x_test[BATCH_SIZE*batch_number : BATCH_SIZE*(batch_number+1)].to(device)
		y_batch = y_test[BATCH_SIZE*batch_number : BATCH_SIZE*(batch_number+1)].to(device)

		y_hat = net.forward(x_batch)

		loss += criterion(y_hat, y_batch).item()
		acc += get_accuracy(y_hat, y_batch).item()
	
	# normalizing the loss and accuracy
	loss = loss/num_test_batches
	acc = acc/num_test_batches

	return loss, acc

async def main():

	# find and connect to workers
	worker_ips = discovery.get_ips(ip=nb_ip)

	# instantiates remote worker objects, with which we can call rpcs on each worker
	workers = [client.RemoteWorker(ip) for ip in worker_ips]

	print('benchmarking workers')

	# start benchmarks in each worker
	benchmark_coros = []
	for w in workers:
		benchmark_coros.append(w.rpcs.benchmark(1000))

	# wait for each worker to finish their benchmark
	benchmark_scores = await asyncio.gather(*benchmark_coros)

	print('sending data to workers')

	# calculates the number of data batches each worker should be assigned
	total_batches = 6000//BATCH_SIZE
	normalizing_factor = total_batches/sum(benchmark_scores)
	data_allocation = [round(normalizing_factor*b) for b in benchmark_scores]

	# assigns data to each worker
	data_allocation_coros = []
	for index, w in enumerate(workers):
		num_batches = data_allocation[index]

		# gets a bunch of random indices of data samples
		indices = torch.randperm(x_train.shape[0])[0: num_batches*BATCH_SIZE]

		x_data = x_train[indices]
		y_data = y_train[indices]

		data_allocation_coros.append(w.rpcs.set_training_data(x_data, y_data))

	# waits for data to be sent to workers
	await asyncio.gather(*data_allocation_coros)

	# evaluate parameters
	loss, acc = val_evaluation(net, x_test, y_test)
	print('network loss and validation prior to training:', loss, acc)

	for i in range(num_global_cycles):
		print('training index:', i, 'out of', num_global_cycles)

		# some workers don't have a GPU and the device that a tensor is on will be serialized, so we've gotta move the network to CPU before transmitting parameters to worker
		net.to('cpu')

		# local updates
		local_update_coros = []
		for w in workers:
			local_update_coros.append(w.rpcs.local_update(list(net.parameters())))

		net.to(device)

		# waits for local updates to complete
		worker_params = await asyncio.gather(*local_update_coros)

		# aggregates parameters
		weights = [d/sum(data_allocation) for d in data_allocation]
		new_params = aggregate_parameters(worker_params, weights)

		# sets the central model to the new parameters
		set_parameters(net, new_params)

		# evaluate new parameters
		loss, acc = val_evaluation(net, x_test, y_test)
		print('network loss and validation:', loss, acc)

if (__name__ == '__main__'):
	asyncio.run(main())