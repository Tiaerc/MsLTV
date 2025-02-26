import logging
import torch
from torch import nn
import torch.nn.functional as F
from model.embedding import Embedding_Left
from model.feature_cross_net import DNN, MoE
from model.transformer import PositionEmbedding, AttentionPoolingLayer, EncoderLayer
from utils.run import get_parameter_number

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(filename)s - %(levelname)s - %(message)s',
                    handlers=[logging.StreamHandler()]
                    )


class MSModel(nn.Module):
    def __init__(self, features, hidden_unit=[128], encode_dim=64, rec_loss_weight=0.01, orthogonal_loss_weight=0.01, sparse_embedding_dim=8,
                 dense_embedding_dim=8, dense_embedding_dim_dapan=8, temperature=0.2, cl_loss_weight_s=0.1, cl_loss_weight_us=0.1, kl_weight=0.1, use_seq=True, device='cuda:0', **kwargs):
        super(MSModel, self).__init__(**kwargs)
        self.features = features
        self.use_seq = use_seq
        self.device = device
        self.temperature = temperature
        self.cl_loss_weight_us = cl_loss_weight_us
        self.kl_weight = kl_weight

        self.embedding_all = Embedding_Left(features, sparse_embedding_dim, dense_embedding_dim, dense_embedding_dim_dapan)
        self.hidden_dim = self.embedding_all.hidden_dim
        self.encoders_specific = nn.ModuleList()
        self.decoders_specific = nn.ModuleList()
        self.views_input_dim = [XXX]
        self.hidden_unit = hidden_unit
        self.rec_loss_weight = rec_loss_weight

        for i in range(len(self.views_input_dim)):
            self.encoders_specific.append(DNN([self.views_input_dim[i]] + self.hidden_unit + [encode_dim]))
            self.decoders_specific.append(DNN([2*encode_dim]+ list(reversed(self.hidden_unit)) + [self.views_input_dim[i]]))
        self.encoder_share = DNN([sum(self.views_input_dim)] + self.hidden_unit + [encode_dim])

        self.output_layer = DNN([7*encode_dim, (7*encode_dim)//2, (7*encode_dim)//4, (7*encode_dim)//8, 3])
        self.mse = nn.MSELoss()
        self.ce = nn.CrossEntropyLoss()
        self.bce = nn.BCEWithLogitsLoss()
        self.kl = nn.KLDivLoss(reduction='batchmean')

        # device
        self.to(self.device)

        # log model info
        model_info = {
            'hidden_dim': self.hidden_dim,
            'params': get_parameter_number(self),
            'device': self.device
        }

        logging.info(f'base model info: {model_info}')

    def forward(self, batch):
        """
        :param batch:
        :return: batch loss
        """
        _, features, labels = self._preprocess(batch)

        _, _, views = self.embedding_all(features)

        encoded_views = []
        for view, encoder in zip(views, self.encoders_specific):
            encoded_views.append(encoder(view))
        encoded_share = self.encoder_share(torch.cat(views, dim=-1))
        logits = self.output_layer(torch.cat([encoded_share]+encoded_views,dim=-1))
        loss = self._ziln_loss(logits, labels)

        for view, encoded_view, decoder in zip(views, encoded_views, self.decoders_specific):
            rebuild = decoder(torch.cat([encoded_share, encoded_view], dim=-1))
            loss += self.rec_loss_weight * self.mse(view, rebuild)

        for encoded_view in encoded_views:
            loss += self.kl_weight * self._kl_div(encoded_share, encoded_view)

        for q in range(len(encoded_views)):
            for k in range(len(encoded_views)):
                if q == k:
                    continue
                loss += self.cl_loss_weight_us * self._contrastive_loss_us(encoded_views[q], encoded_views[k])
                

        p, value = self._ziln_predict(logits)
        return_dict = {
            'logit': logits,
            'loss': loss,
            'prediction_p': p,
            'prediction_v': value
        }
        return return_dict


    def _preprocess(self, batch):
        configs, features, labels = batch
        for k, v in features.items():
            if isinstance(v, torch.Tensor):
                features[k] = v.to(self.device)
        labels = labels.to(self.device)
        return configs, features, labels


    def _kl_div(self, p_output, q_output):
        p_output = F.log_softmax(p_output)
        q_output = F.softmax(q_output)
        return self.kl(p_output, q_output)

    def _encode(self, inputs):
        # config features
        game_id_hash = self.game_id_embedding_shared(inputs['XXXX'])
        media_type_hash = self.game_id_embedding_shared(inputs['XXXX'])
        media_id_hash = self.game_id_embedding_shared(inputs['XXXX'])

        # sparse
        sparse = []
        for k, v in self.sparse_embedding_dict.items():
            sparse.append(v(inputs[k]))
        sparse_dapan = []
        for k, v in self.sparse_embedding_dict_dapan.items():
            sparse_dapan.append(v(inputs[k]))

        # dense
        dense = []
        for k, v in self.dense_embedding_dict.items():
            dense.append(v(inputs[k]))
        dense_dapan = []
        for k, v in self.dense_embedding_dict_dapan.items():
            dense_dapan.append(v(inputs[k]))

        # pooling query
        pooling_query = torch.cat(sparse_dapan + [game_id_hash], dim=-1)

        # dense dapan pooling
        dapan_query = self.dapan_pooling_query_convert(pooling_query)
        dense_dapan_pooling = self.dapan_pooling_layer(dapan_query, torch.stack(dense_dapan, dim=1))

        # seq
        seq = []
        if self.use_seq:
            online_time_emb = self.online_time_emb_layer(inputs['XXXX'])
            payment_emb = self.payment_emb_layer(inputs['XXXX'])

            reg_game_emb = self.game_id_embedding_shared(inputs['XXXX'])
            act_game_emb = self.game_id_embedding_shared(inputs['XXXX'])
            pay_game_emb = self.game_id_embedding_shared(inputs['XXXX'])
            act_emb = (act_game_emb + online_time_emb) / 2
            pay_emb = (pay_game_emb + payment_emb) / 2

            seq_query = self.seq_pooling_query_convert(pooling_query)
            seq_embeddings = [reg_game_emb, act_emb, pay_emb]
            transformer_layers = 1
            for seq_embedding in seq_embeddings:
                x_first = self.pos_emb_layer(seq_embedding)
                for i in range(transformer_layers):
                    x_first = self.enc_layers[i](x_first)
                x_first = self.seq_pooling_layer(seq_query, x_first)
                seq.append(x_first)

        return torch.cat([game_id_hash, media_id_hash, media_type_hash], dim=-1), torch.cat(sparse, dim=-1), torch.cat(sparse_dapan, dim=-1), torch.cat(dense, dim=-1), torch.cat([dense_dapan_pooling], dim=-1), torch.cat(seq, dim=-1)

    def _decode(self, hidden):
        output = self.cross_layer(hidden)
        return output

    @staticmethod
    def _ziln_loss(logits, label, label_clip_val=0., class_threshold=0.):
        if label_clip_val > 0:
            label = torch.clamp(label, min=0, max=label_clip_val)

        p_pred = logits[:, 0]
        p_ground = (label > class_threshold).to(torch.float32)

        cls_loss = nn.BCEWithLogitsLoss(reduction='none')(p_pred, p_ground)

        mu = logits[:, 1]
        sigma = torch.maximum(nn.Softplus()(logits[:, 2]), torch.sqrt(torch.tensor(torch.finfo(torch.float32).eps)))

        safe_labels = p_ground * label + (1 - p_ground) * torch.ones_like(label)
        reg_loss = 0. - p_ground * torch.distributions.LogNormal(mu, sigma).log_prob(safe_labels)

        return torch.mean(cls_loss + reg_loss, dim=0)

    @staticmethod
    def _ziln_predict(logits, pred_clip_val=0):
        p, mu, sigma = logits[:, 0], logits[:, 1], logits[:, 2]
        p = torch.nn.Sigmoid()(p)
        sigma = torch.nn.Softplus()(sigma)

        preds = (p * torch.exp(mu + 0.5 * torch.square(sigma)))

        if pred_clip_val > 0:
            preds = torch.clamp(preds, min=0, max=pred_clip_val)

        return p, preds

    def _contrastive_loss_us(self, q, k):
        """
        info nce loss
        :return:
        """
        batch_size = q.shape[0]
        preds = torch.div(torch.matmul(q, k.T), self.temperature)
        labels = torch.arange(batch_size, device=q.device, dtype=torch.long)
        return self.ce(preds, labels)


    def load_weights(self, path):
        self.load_state_dict(torch.load(path))
