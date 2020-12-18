import sys
sys.path.append('/content/drive/MyDrive/trajectory-prediction-GRIP-current_approach/')
import argparse
import os 
import sys
import numpy as np 
import torch
import torch.optim as optim
from model import Model
from xin_feeder_baidu import Feeder
from datetime import datetime
import random
import itertools
#from visualization.wandb_utils import init_wandb, save_model_wandb, log_losses, log_metrics, log_summary 

CUDA_VISIBLE_DEVICES='0'
os.environ["CUDA_VISIBLE_DEVICES"] = CUDA_VISIBLE_DEVICES

def seed_torch(seed=0):
	random.seed(seed)
	os.environ['PYTHONHASHSEED'] = str(seed)
	np.random.seed(seed)
	torch.manual_seed(seed)
	torch.cuda.manual_seed(seed)
	torch.cuda.manual_seed_all(seed) # if you are using multi-GPU.
	torch.backends.cudnn.benchmark = False
	torch.backends.cudnn.deterministic = True
seed_torch()

max_x = 1. 
max_y = 1. 
history_frames = 6 # 3 second * 2 frame/second
future_frames = 6 # 3 second * 2 frame/second

batch_size_train = 64 
batch_size_val = 32
batch_size_test = 1
total_epoch = 300
base_lr = 0.01
lr_decay_epoch = 5
dev = 'cuda:0' 
work_dir = 'trained_models'
log_file = os.path.join(work_dir,'log_test.txt')
log_file_epoch = os.path.join(work_dir,'log_test_epoch.txt')
test_result_file = 'prediction_result.txt'

criterion = torch.nn.SmoothL1Loss()

if not os.path.exists(work_dir):
	os.makedirs(work_dir)

# For saving to lof file
def my_print(pra_content):
	with open(log_file, 'a') as writer:
		print(pra_content)
		writer.write(pra_content+'\n')
def my_print_epoch(pra_content):
	with open(log_file_epoch, 'a') as writer:
		#print(pra_content)
		writer.write(pra_content+'\n')

# For validation results
def display_result_val(pra_results, pra_pref='Train_epoch', epoch_no=0, use_wandb=False):
	all_overall_sum_list, all_overall_num_list, val_metrics = pra_results
	overall_sum_time = np.sum(all_overall_sum_list, axis=0)
	overall_num_time = np.sum(all_overall_num_list, axis=0)
	overall_loss_time = (overall_sum_time / overall_num_time)**0.5
	overall_log = '|{}|[{}] All_All: {}'.format(datetime.now(), pra_pref, ' '.join(['{:.3f}'.format(x) for x in list(overall_loss_time) + [np.sum(overall_loss_time)/6.]]))
	my_print_epoch(overall_log)
	my_print(overall_log)
	val_metrics['overall_unweighted_ade'] = np.sum(overall_loss_time)/6.
	val_metrics['overall_unweighted_fde'] = overall_loss_time[5]
	if use_wandb:
		log_metrics(val_metrics, "validation", epoch_no)
	return val_metrics


# For dislaying result on screen
def display_result(pra_results, pra_pref='Train_epoch'):
	all_overall_sum_list, all_overall_num_list = pra_results
	overall_sum_time = np.sum(all_overall_sum_list, axis=0)
	overall_num_time = np.sum(all_overall_num_list, axis=0)
	overall_loss_time = (overall_sum_time / overall_num_time)
	overall_log = '|{}|[{}] All_All: {}'.format(datetime.now(), pra_pref, ' '.join(['{:.3f}'.format(x) for x in list(overall_loss_time) + [np.sum(overall_loss_time)/6.]]))
	my_print_epoch(overall_log)
	my_print(overall_log)
	return overall_loss_time

# To save the model by the name of pra_epoch in work_dir
def my_save_model(pra_model, pra_epoch):
	path = '{}/model_epoch_{:04}.pt'.format(work_dir, pra_epoch)
	torch.save(
		{
			'xin_graph_seq2seq_model': pra_model.state_dict(),
		}, 
		path)
	print('Successfull saved to {}'.format(path))

# To load the saved model from work_dir
def my_load_model(pra_model, pra_path):
	checkpoint = torch.load(pra_path)
	pra_model.load_state_dict(checkpoint['xin_graph_seq2seq_model'])
	print('Successfull loaded from {}'.format(pra_path))
	return pra_model

# custom dataloader and dataset class
def data_loader(pra_path, pra_batch_size=128, pra_shuffle=False, pra_drop_last=False, train_val_test='train'):
	feeder = Feeder(data_path=pra_path, graph_args=graph_args, train_val_test=train_val_test)
	loader = torch.utils.data.DataLoader(
		dataset=feeder,
		batch_size=pra_batch_size,
		shuffle=pra_shuffle,
		drop_last=pra_drop_last, 
		num_workers=0,
		)
	return loader
	
def preprocess_data(pra_data, pra_rescale_xy):
	# pra_data: (N, C, T, V)
	# C = 11: [frame_id, object_id, object_type, position_x, position_y, position_z, object_length, pbject_width, pbject_height, heading] + [mask]	
	feature_id = [3, 4, 2, 7]
	ori_data = pra_data[:,feature_id].detach()
	data = ori_data.detach().clone()

	new_mask = (data[:, :2, 1:]!=0) * (data[:, :2, :-1]!=0) 
	# data contains velocity
	data[:, :2, 1:] = (data[:, :2, 1:] - data[:, :2, :-1]).float() * new_mask.float()
	data[:, :2, 0] = 0	


	# # small vehicle: 1, big vehicles: 2, pedestrian 3, bicycle: 4, others: 5
	object_type = pra_data[:,2:3]

	data = data.float().to(dev)
	ori_data = ori_data.float().to(dev)
	object_type = object_type.to(dev) #type
	data[:,:2] = data[:,:2] / pra_rescale_xy

	return data, ori_data, object_type # data contains velocity, ori_data contains position and object_type contains type of the object
	
def compute_RMSE(pra_pred, pra_GT, pra_mask, pra_error_order=2):
	#print(pra_mask.shape)
	#print(pra_pred.shape)
	pra_mask = pra_mask.to(dev)
	pred = pra_pred * pra_mask # (N, C, T, V)=(N, 2, 6, 120)
	GT = pra_GT * pra_mask # (N, C, T, V)=(N, 2, 6, 120)
	if pra_error_order ==2 :
		x2y2 = torch.sum(torch.abs(pred - GT)**pra_error_order, dim=1) # x^2+y^2, (N, C, T, V)->(N, T, V)=(N, 6, 120)
	else :
		x2y2 = torch.sum(torch.abs(pred - GT)**pra_error_order, dim=1)
	overall_sum_time = x2y2.sum(dim=-1) # (N, T, V) -> (N, T)=(N, 6)
	overall_mask = pra_mask.sum(dim=1).sum(dim=-1) # (N, C, T, V) -> (N, T)=(N, 6)
	overall_num = overall_mask

	return overall_sum_time, overall_num, x2y2
def compute_RMSE_old(pra_pred, pra_GT, pra_mask, pra_error_order=2):
	pred = pra_pred * pra_mask # (N, C, T, V)=(N, 2, 6, 120)
	GT = pra_GT * pra_mask # (N, C, T, V)=(N, 2, 6, 120)
	
	x2y2 = torch.sum(torch.abs(pred - GT)**pra_error_order, dim=1) # x^2+y^2, (N, C, T, V)->(N, T, V)=(N, 6, 120)
	overall_sum_time = x2y2.sum(dim=-1) # (N, T, V) -> (N, T)=(N, 6)
	overall_mask = pra_mask.sum(dim=1).sum(dim=-1) # (N, C, T, V) -> (N, T)=(N, 6)
	overall_num = overall_mask 

	return overall_sum_time, overall_num, x2y2


def train_model(pra_model, pra_data_loader, pra_optimizer, pra_epoch_log):
	# pra_model.to(dev)
	pra_model.train()
	rescale_xy = torch.ones((1,2,1,1)).to(dev)
	rescale_xy[:,0] = max_x
	rescale_xy[:,1] = max_y
	overall_loss = 0
	losses = {}
	# train model using training data
	for iteration, (_,ori_data, A, _) in enumerate(pra_data_loader):
		# print(iteration, ori_data.shape, A.shape)
		# ori_data: (N, C, T, V)
		# C = 11: [frame_id, object_id, object_type, position_x, position_y, position_z, object_length, pbject_width, pbject_height, heading] + [mask]
		if(iteration>59):
			continue
		data, no_norm_loc_data, object_type = preprocess_data(ori_data, rescale_xy)
		for now_history_frames in range(6,min(7, data.shape[-2])):
			input_data = data[:,:,:now_history_frames,:] # (N, C, T, V)=(N, 4, 6, 120)
			output_loc_GT = data[:,:2,now_history_frames:,:] # (N, C, T, V)=(N, 2, 6, 120)
			cat_mask = ori_data[:,2:3, now_history_frames:, :] # (N, C, T, V)=(N, 1, 6, 120)
			output_mask = data[:,-1:,now_history_frames:,:]*(data[:,0:1,5:6,:]!=0).float().to(dev) * (((cat_mask==1)+(cat_mask==3)+(cat_mask==4))>0).float().to(dev)# (N, C, T, V)=(N, 1, 6, 120)
			
			A = A.float().to(dev)
		
			predicted = pra_model(pra_x=input_data, pra_A=A, pra_pred_length=output_loc_GT.shape[-2], pra_teacher_forcing_ratio=0, pra_teacher_location=output_loc_GT) # (N, C, T, V)=(N, 2, 6, 120)
			#print(predicted)
			#print(output_loc_GT)
			########################################################
			# Compute loss for training
			########################################################
			# We use abs to compute loss to backward update weights
			# (N, T), (N, T)
			overall_sum_time, overall_num, _ = compute_RMSE(predicted, output_loc_GT, output_mask, pra_error_order=1)
			# overall_loss
			total_loss = torch.sum(overall_sum_time) / torch.max(torch.sum(overall_num), torch.ones(1,).to(dev)) #(1,)
			overall_loss = overall_loss + total_loss.data.item()
			now_lr = [param_group['lr'] for param_group in pra_optimizer.param_groups][0]
			my_print('|{}|{:>20}|\tIteration:{:>5}|\tLoss:{:.8f}|lr: {}|'.format(datetime.now(), pra_epoch_log, iteration, total_loss.data.item(),now_lr))
			
			pra_optimizer.zero_grad()
			total_loss.backward()
			pra_optimizer.step()

	losses['Overall_loss'] = overall_loss
	return losses
		

def val_model(pra_model, pra_data_loader):
	# pra_model.to(dev)
	pra_model.eval()
	rescale_xy = torch.ones((1,2,1,1)).to(dev)
	rescale_xy[:,0] = max_x
	rescale_xy[:,1] = max_y
	all_overall_sum_list = []
	all_overall_num_list = []

	all_car_sum_list = []
	all_car_num_list = []
	all_human_sum_list = []
	all_human_num_list = []
	all_bike_sum_list = []
	all_bike_num_list = []
	losses = {}
	# train model using training data
	for iteration, (rev_angle_mat, ori_data, A, _) in enumerate(pra_data_loader):
		# data: (N, C, T, V)
		# C = 11: [frame_id, object_id, object_type, position_x, position_y, position_z, object_length, pbject_width, pbject_height, heading] + [mask]
		data, no_norm_loc_data, _ = preprocess_data(ori_data, rescale_xy)

		for now_history_frames in range(6, 7):
			input_data = data[:,:,:now_history_frames,:] # (N, C, T, V)=(N, 4, 6, 120)
			output_loc_GT = data[:,:2,now_history_frames:,:] # (N, C, T, V)=(N, 2, 6, 120)
			output_mask = data[:,-1:,now_history_frames:,:] * (data[:,0:1,5:6,:]!=0).float().to('cuda:0') # (N, C, T, V)=(N, 1, 6, 120)
			leftover_mask = data[:,-1:,now_history_frames:,:] - output_mask
			left_vehicles = ((no_norm_loc_data[:,0,now_history_frames-1,:] * no_norm_loc_data[:,0,now_history_frames-2,:])!=0).float().to('cuda:0')
			#print(left_vehicles.shape)
			#print(A.shape)
			left_veh_mat = torch.einsum('nv,nvw->nvw',(left_vehicles,A[:,5].float().to('cuda:0')))
			left_veh_mat = torch.einsum('nw,nvw->nvw',(1-left_vehicles,left_veh_mat))
			
			Dl = torch.max(torch.sum(left_veh_mat,axis=1,keepdims=True), torch.ones(1,).to(dev))**(-1)
			left_veh_mat = left_veh_mat*Dl
			#output_mask = output_mask*(data[:,0:1,5:6,:]!=0).float().to('cuda:0')
			ori_output_loc_GT = no_norm_loc_data[:,:2,now_history_frames:,:]
			ori_output_last_loc = no_norm_loc_data[:,:2,now_history_frames-1:now_history_frames,:]

			# for category
			cat_mask = ori_data[:,2:3, now_history_frames:, :] # (N, C, T, V)=(N, 1, 6, 120)
			#last_data = torch.zeros((32,2,1,120))
			last_data = data[:,:,history_frames-1:history_frames,:]*(no_norm_loc_data[:,:,history_frames-2:history_frames-1,:]!=0).float() + data[:,:,history_frames:history_frames+1,:]*(no_norm_loc_data[:,:,history_frames-2:history_frames-1,:]==0).float()   
			#		last_data[i,1,0,j] = data[i,1,history_frames-1,j] if no_norm_loc_data[i,1,history_frames-2,j]!=0 else data[i,1,history_frames,j]   
			#predicted = torch.cat((last_data,last_data,last_data,last_data,last_data,last_data),axis=2)
			
			A = A.float().to(dev)
			predicted = pra_model(pra_x=input_data, pra_A=A, pra_pred_length=output_loc_GT.shape[-2], pra_teacher_forcing_ratio=0, pra_teacher_location=output_loc_GT) # (N, C, T, V)=(N, 2, 6, 120)
			#print(rev_angle_mat.shape)
			#print(ori_output_loc_GT.shape)
			ori_output_loc_GT = torch.einsum('nabv,nbtv->natv',rev_angle_mat.float().to('cuda:0'),ori_output_loc_GT.float())
			ori_output_last_loc = torch.einsum('nabv,nbtv->natv',rev_angle_mat.float().to('cuda:0'),ori_output_last_loc.float())
			########################################################
			# Compute details for training
			########################################################
			predicted = predicted[:,:2].to('cuda:0')*rescale_xy
			#mask2 is (n,v)
			predicted = torch.einsum('nabv,nbtv->natv',rev_angle_mat.float().to('cuda:0'),predicted)
			mask2 = (no_norm_loc_data[:,0,now_history_frames-1,:]!=0).float() - (torch.sum(left_veh_mat,axis=1)!=0).float() - left_vehicles.float()

			avg2 = (predicted*output_mask).sum(axis=-1)*(torch.max(output_mask.sum(axis=-1), torch.ones(1,).to(dev))**(-1))
			#print(mask2.shape)
			#print(avg2.shape)
			pred_left_left_veh = torch.einsum('nct,nv->nctv',(avg2,mask2))
			# output_loc_GT = output_loc_GT*rescale_xy
			pred_left_veh = torch.einsum('nctv,nvw->nctw',predicted,left_veh_mat)+pred_left_left_veh
			predicted = predicted*output_mask + pred_left_veh*leftover_mask
			output_mask = output_mask#+leftover_mask
			for ind in range(1, predicted.shape[-2]):
				predicted[:,:,ind] = torch.sum(predicted[:,:,ind-1:ind+1], dim=-2)
			predicted += ori_output_last_loc
			# predicted is n2tv and rev_angle_mat is nabv
			### overall dist
			# overall_sum_time, overall_num, x2y2 = compute_RMSE(predicted, output_loc_GT, output_mask)		
			overall_sum_time, overall_num, x2y2 = compute_RMSE(predicted, ori_output_loc_GT, output_mask)		
			# all_overall_sum_list.extend(overall_sum_time.detach().cpu().numpy())
			all_overall_num_list.extend(overall_num.detach().cpu().numpy())
			# x2y2 (N, 6, 39)
			now_x2y2 = x2y2.detach().cpu().numpy()
			now_x2y2 = now_x2y2.sum(axis=-1)
			all_overall_sum_list.extend(now_x2y2)

			### car dist
			car_mask = (((cat_mask==1)+(cat_mask==3))>0).float().to(dev)
			car_mask = output_mask * car_mask
			car_sum_time, car_num, car_x2y2 = compute_RMSE(predicted, ori_output_loc_GT, car_mask)		
			all_car_num_list.extend(car_num.detach().cpu().numpy())
			# x2y2 (N, 6, 39)
			car_x2y2 = car_x2y2.detach().cpu().numpy()
			car_x2y2 = car_x2y2.sum(axis=-1)
			all_car_sum_list.extend(car_x2y2)

			### human dist
			human_mask = (cat_mask==2).float().to(dev)
			human_mask = output_mask * human_mask
			human_sum_time, human_num, human_x2y2 = compute_RMSE(predicted, ori_output_loc_GT, human_mask)		
			all_human_num_list.extend(human_num.detach().cpu().numpy())
			# x2y2 (N, 6, 39)
			human_x2y2 = human_x2y2.detach().cpu().numpy()
			human_x2y2 = human_x2y2.sum(axis=-1)
			all_human_sum_list.extend(human_x2y2)

			### bike dist
			bike_mask = (cat_mask==4).float().to(dev)
			bike_mask = output_mask * bike_mask
			bike_sum_time, bike_num, bike_x2y2 = compute_RMSE(predicted, ori_output_loc_GT, bike_mask)		
			all_bike_num_list.extend(bike_num.detach().cpu().numpy())
			# x2y2 (N, 6, 39)
			bike_x2y2 = bike_x2y2.detach().cpu().numpy()
			bike_x2y2 = bike_x2y2.sum(axis=-1)
			all_bike_sum_list.extend(bike_x2y2)

	
	result_car = display_result([np.array(all_car_sum_list), np.array(all_car_num_list)], pra_pref='car')
	result_human = display_result([np.array(all_human_sum_list), np.array(all_human_num_list)], pra_pref='human')
	result_bike = display_result([np.array(all_bike_sum_list), np.array(all_bike_num_list)], pra_pref='bike')
	losses['result_ade_car'] = np.sum(result_car)/6.
	losses['result_ade_human'] = np.sum(result_human)/6.
	losses['result_ade_bike'] = np.sum(result_bike)/6.
	losses['result_fde_car'] = result_car[5]
	losses['result_fde_human'] = result_human[5]
	losses['result_fde_bike'] = result_bike[5]
	result = (0.2*result_car + 0.58*result_human + 0.22*result_bike)
	losses['result_wsade'] = np.sum(result)/6.
	losses['result_wsfde'] = result[5]
	
	overall_log = '|{}|[{}] All_All: {}'.format(datetime.now(), 'WS', ' '.join(['{:.3f}'.format(x) for x in list(result) + [np.sum(result)/6.]]))
	my_print(overall_log)
	overall_log_epoch = '|{}|[{}] All_All: {}'.format(datetime.now(), 'WS', ' '.join(['{:.3f}'.format(x) for x in list(result) + [np.sum(result)/6.]]))
	my_print_epoch(overall_log_epoch)
	all_overall_sum_list = np.array(all_overall_sum_list)
	all_overall_num_list = np.array(all_overall_num_list)
	return all_overall_sum_list, all_overall_num_list, losses



def test_model(pra_model, pra_data_loader):
	# pra_model.to(dev)
	pra_model.eval()
	rescale_xy = torch.ones((1,2,1,1)).to(dev)
	rescale_xy[:,0] = max_x
	rescale_xy[:,1] = max_y
	all_overall_sum_list = []
	all_overall_num_list = []
	with open(test_result_file, 'w') as writer:
		# train model using training data
		for iteration, (ori_data, A, mean_xy) in enumerate(pra_data_loader):
			# data: (N, C, T, V)
			# C = 11: [frame_id, object_id, object_type, position_x, position_y, position_z, object_length, pbject_width, pbject_height, heading] + [mask]
			data, no_norm_loc_data, _ = preprocess_data(ori_data, rescale_xy)
			input_data = data[:,:,:history_frames,:] # (N, C, T, V)=(N, 4, 6, 120)
			output_mask = data[:,-1,-1,:] # (N, V)=(N, 120)
			# print(data.shape, A.shape, mean_xy.shape, input_data.shape)

			ori_output_last_loc = no_norm_loc_data[:,:2,history_frames-1:history_frames,:]
		
			A = A.float().to(dev)
			predicted = pra_model(pra_x=input_data, pra_A=A, pra_pred_length=future_frames, pra_teacher_forcing_ratio=0, pra_teacher_location=None) # (N, C, T, V)=(N, 2, 6, 120)
			predicted = predicted *rescale_xy 

			for ind in range(1, predicted.shape[-2]):
				predicted[:,:,ind] = torch.sum(predicted[:,:,ind-1:ind+1], dim=-2)
			predicted += ori_output_last_loc

			now_pred = predicted.detach().cpu().numpy() # (N, C, T, V)=(N, 2, 6, 120)
			now_mean_xy = mean_xy.detach().cpu().numpy() # (N, 2)
			now_ori_data = ori_data.detach().cpu().numpy() # (N, C, T, V)=(N, 11, 6, 120)
			now_mask = now_ori_data[:, -1, -1, :] # (N, V)
			
			now_pred = np.transpose(now_pred, (0, 2, 3, 1)) # (N, T, V, 2)
			now_ori_data = np.transpose(now_ori_data, (0, 2, 3, 1)) # (N, T, V, 11)
			
			# print(now_pred.shape, now_mean_xy.shape, now_ori_data.shape, now_mask.shape)

			for n_pred, n_mean_xy, n_data, n_mask in zip(now_pred, now_mean_xy, now_ori_data, now_mask):
				# (6, 120, 2), (2,), (6, 120, 11), (120, )
				num_object = np.sum(n_mask).astype(int)
				# only use the last time of original data for ids (frame_id, object_id, object_type)
				# (6, 120, 11) -> (num_object, 3)
				n_dat = n_data[-1, :num_object, :3].astype(int)
				for time_ind, n_pre in enumerate(n_pred[:, :num_object], start=1):
					# (120, 2) -> (n, 2)
					# print(n_dat.shape, n_pre.shape)
					for info, pred in zip(n_dat, n_pre+n_mean_xy):
						information = info.copy()
						information[0] = information[0] + time_ind
						result = ' '.join(information.astype(str)) + ' ' + ' '.join(pred.astype(str)) + '\n'
						# print(result)
						writer.write(result)


def run_trainval(pra_model, pra_traindata_path, pra_testdata_path, use_wandb):
	loader_train = data_loader(pra_traindata_path, pra_batch_size=batch_size_train, pra_shuffle=True, pra_drop_last=True, train_val_test='train')
	#loader_test = data_loader(pra_testdata_path, pra_batch_size=batch_size_train, pra_shuffle=True, pra_drop_last=True, train_val_test='all')

	# evaluate on testing data (observe 5 frame and predict 1 frame)
	loader_val = data_loader(pra_traindata_path, pra_batch_size=batch_size_val, pra_shuffle=False, pra_drop_last=False, train_val_test='val') 
	optimizer = optim.Adam(
		[{'params':pra_model.parameters()},],) # lr = 0.0001)
	
	best_wsade = 100
	best_wsfde = 100
	best_ade_car = 100
	best_ade_human = 100
	best_ade_bike = 100
	summary = {}
	for now_epoch in range(total_epoch):
		all_loader_train = loader_train
		
		my_print('#######################################Train')
		train_loss = train_model(pra_model, all_loader_train, pra_optimizer=optimizer, pra_epoch_log='Epoch:{:>4}/{:>4}'.format(now_epoch, total_epoch))
		if use_wandb:
			log_losses(train_loss, "train", now_epoch)
		

		my_print('#######################################Test')
		my_print_epoch('#######################################Test ' + str(now_epoch))
		val_metrics = display_result_val(
			val_model(pra_model, loader_val),
			pra_pref='{}_Epoch{}'.format('Test', now_epoch),
			epoch_no=now_epoch,
			use_wandb=use_wandb
		)
		if best_wsade<val_metrics['result_wsade']:
			my_save_model(pra_model, now_epoch)
			summary['Epoch_no'] = now_epoch
		best_wsade =min(best_wsade, val_metrics['result_wsade'])
		best_wsfde =min(best_wsfde, val_metrics['result_wsfde'])
		best_ade_car =min(best_ade_car, val_metrics['result_ade_car'])
		best_ade_bike =min(best_ade_bike, val_metrics['result_ade_bike'])
		best_ade_human =min(best_ade_human, val_metrics['result_ade_human'])
		summary['best_WSADE'] = best_wsade
		summary['best_WSFDE'] = best_wsfde
		summary['best_ADE_CAR'] = best_ade_car
		summary['best_ADE_BIKE'] = best_ade_bike
		summary['best_ADE_HUMAN'] = best_ade_human
		if use_wandb:
			log_summary(summary)

def run_test(pra_model, pra_data_path):
	loader_test = data_loader(pra_data_path, pra_batch_size=batch_size_test, pra_shuffle=False, pra_drop_last=False, train_val_test='test')
	test_model(pra_model, loader_test)



if __name__ == '__main__':
	parser = argparse.ArgumentParser(description="PECNet")
	parser.add_argument("--num_workers", "-nw", type=int, default=0)
	parser.add_argument("--gpu_index", "-gi", type=int, default=0)
	parser.add_argument("--config_filename", "-cfn", type=str, default="optimal.yaml")
	parser.add_argument("--version", "-v", type=str, default="GRIP_social_model")
	parser.add_argument("--verbose", action="store_true")
	parser.add_argument("-s", "--seed", default=42, help="Random seed")
	parser.add_argument("-w", "--wandb", action="store_true", help="Log to wandb or not")
	parser.add_argument("-d", "--dataset", default="drone", help="The datset to train the model on (ETH_UCY or drone)")
	parser.add_argument("-n", "--no_of_epochs", default=300, help="No of epochs to run")
	args = parser.parse_args()
	total_epoch = int(args.no_of_epochs)
	torch.autograd.set_detect_anomaly(True)
	graph_args={'max_hop':1, 'num_node':400}
	model = Model(in_channels=4, graph_args=graph_args, edge_importance_weighting=True)
	model.to(dev)
	if args.wandb:
		init_wandb(graph_args, model, args)
	#pretrained_model_path = 'trained_models/model_epoch_0049.pt'
	#model = my_load_model(model, pretrained_model_path)

	# train and evaluate model
	run_trainval(model, pra_traindata_path='/content/drive/MyDrive/trajectory-prediction-GRIP-current_approach/train_data.pkl', pra_testdata_path='test_data.pkl', use_wandb=args.wandb)
	
	# run_test(model, './test_data.pkl')
	
		
		

