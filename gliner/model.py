import os
import json
import warnings
from pathlib import Path
from typing import Optional, Union, Dict, List

from transformers import AutoTokenizer, AutoConfig

import torch
from torch import nn
import numpy as np

from .modeling.base import BaseModel, SpanModel, TokenModel
from .onnx.model import BaseORTModel, SpanORTModel, TokenORTModel
from .data_processing import SpanProcessor, TokenProcessor
from .data_processing.tokenizer import WordsSplitter
from .decoding import SpanDecoder, TokenDecoder
from .config import GLiNERConfig

from huggingface_hub import PyTorchModelHubMixin, snapshot_download

class GLiNER(nn.Module, PyTorchModelHubMixin):
    def __init__(self, config: GLiNERConfig, 
                        model: Optional[Union[BaseModel, BaseORTModel]] = None,
                        tokenizer: Optional[Union[str, AutoTokenizer]] = None, 
                        words_splitter: Optional[Union[str, WordsSplitter]] = None, 
                        encoder_from_pretrained: bool = True):
        super().__init__()
        self.config = config

        if tokenizer is None:
            tokenizer = AutoTokenizer.from_pretrained(config.model_name)

        if config.vocab_size !=-1 and config.vocab_size!=len(tokenizer):
            warnings.warn(f"""Vocab size of the model ({config.vocab_size}) does't match length of tokenizer ({len(tokenizer)}). 
                            You should to consider manually add new tokens to tokenizer or to load tokenizer with added tokens.""")
        if words_splitter is None:
            words_splitter = WordsSplitter(config.words_splitter_type)

        if config.span_mode == "token_level":
            if model is None:
                self.model = TokenModel(config, encoder_from_pretrained)
            else:
                self.model = model
            self.data_processor = TokenProcessor(config, tokenizer, words_splitter)
            self.decoder = TokenDecoder(config)
        else:
            if model is None:
                self.model = SpanModel(config, encoder_from_pretrained)
            else:
                self.model = model
            self.data_processor = SpanProcessor(config, tokenizer, words_splitter)
            self.decoder = SpanDecoder(config)

        if isinstance(self.model, BaseORTModel):
            self.onnx_model = True
        else:
            self.onnx_model = True

    def forward(self, *args, **kwargs):
        output = self.model(*args, **kwargs)
        return output

    def resize_token_embeddings(self, add_tokens, 
                                    set_class_token_index = True, 
                                    add_tokens_to_tokenizer = True, 
                                    pad_to_multiple_of=None) -> nn.Embedding:
        if set_class_token_index:
            self.config.class_token_index = len(self.data_processor.transformer_tokenizer)+1
        if add_tokens_to_tokenizer:
            self.data_processor.transformer_tokenizer.add_tokens(add_tokens)
        new_num_tokens = len(self.data_processor.transformer_tokenizer)
        model_embeds = self.model.token_rep_layer.resize_token_embeddings(new_num_tokens, pad_to_multiple_of)
        # update vocab size
        self.config.vocab_size = model_embeds.num_embeddings
        if self.config.encoder_config is not None:
            self.config.encoder_config.vocab_size = model_embeds.num_embeddings
        return model_embeds

    def prepare_model_inputs(self, texts: str, labels: str):
        all_tokens = []
        all_start_token_idx_to_text_idx = []
        all_end_token_idx_to_text_idx = []

        for text in texts:
            tokens = []
            start_token_idx_to_text_idx = []
            end_token_idx_to_text_idx = []
            for token, start, end in self.data_processor.words_splitter(text):
                tokens.append(token)
                start_token_idx_to_text_idx.append(start)
                end_token_idx_to_text_idx.append(end)
            all_tokens.append(tokens)
            all_start_token_idx_to_text_idx.append(start_token_idx_to_text_idx)
            all_end_token_idx_to_text_idx.append(end_token_idx_to_text_idx)

        input_x = [{"tokenized_text": tk, "ner": None} for tk in all_tokens]
        raw_batch = self.data_processor.collate_raw_batch(input_x, labels)
        raw_batch["all_start_token_idx_to_text_idx"] = all_start_token_idx_to_text_idx
        raw_batch["all_end_token_idx_to_text_idx"] = all_end_token_idx_to_text_idx

        model_input = self.data_processor.collate_fn(raw_batch, prepare_labels=False)
        model_input.update({"span_idx": raw_batch['span_idx'] if 'span_idx' in raw_batch else None, 
                            "span_mask": raw_batch["span_mask"] if 'span_mask' in raw_batch else None,
                            "text_lengths": raw_batch['seq_length']})
        
        if not self.onnx_model:
            device = next(self.model.parameters()).device
            for key in model_input:
                if model_input[key] is not None and isinstance(model_input[key], torch.Tensor):
                    model_input[key] = model_input[key].to(device)

        return model_input, raw_batch
    
    def predict_entities(self, text, labels, flat_ner=True, threshold=0.5, multi_label=False):
        return self.batch_predict_entities(
            [text], labels, flat_ner=flat_ner, threshold=threshold, multi_label=multi_label
        )[0]

    @torch.no_grad()
    def batch_predict_entities(self, texts, labels, flat_ner=True, threshold=0.5, multi_label=False):
        """
        Predict entities for a batch of texts.
        texts:  List of texts | List[str]
        labels: List of labels | List[str]
        ...
        """

        model_input, raw_batch = self.prepare_model_inputs(texts, labels)

        model_output = self.model(**model_input)[0]

        if not isinstance(model_output, torch.Tensor):
            model_output = torch.from_numpy(model_output)

        outputs = self.decoder.decode(raw_batch['tokens'], raw_batch['id_to_classes'], 
                    model_output, flat_ner=flat_ner, threshold=threshold, multi_label=multi_label)

        all_entities = []
        for i, output in enumerate(outputs):
            start_token_idx_to_text_idx = raw_batch['all_start_token_idx_to_text_idx'][i]
            end_token_idx_to_text_idx = raw_batch['all_end_token_idx_to_text_idx'][i]
            entities = []
            for start_token_idx, end_token_idx, ent_type, ent_score in output:
                start_text_idx = start_token_idx_to_text_idx[start_token_idx]
                end_text_idx = end_token_idx_to_text_idx[end_token_idx]
                entities.append({
                    "start": start_token_idx_to_text_idx[start_token_idx],
                    "end": end_token_idx_to_text_idx[end_token_idx],
                    "text": texts[i][start_text_idx:end_text_idx],
                    "label": ent_type,
                    "score": ent_score
                })
            all_entities.append(entities)

        return all_entities

    def set_sampling_params(self, max_types, shuffle_types, random_drop, max_neg_type_ratio, max_len):
        """
        Sets sampling parameters on the given model.

        Parameters:
        - model: The model object to update.
        - max_types: Maximum types parameter.
        - shuffle_types: Boolean indicating whether to shuffle types.
        - random_drop: Boolean indicating whether to randomly drop elements.
        - max_neg_type_ratio: Maximum negative type ratio.
        - max_len: Maximum length parameter.
        """
        self.config.max_types = max_types
        self.config.shuffle_types = shuffle_types
        self.config.random_drop = random_drop
        self.config.max_neg_type_ratio = max_neg_type_ratio
        self.config.max_len = max_len

    def save_pretrained(
            self,
            save_directory: Union[str, Path],
            *,
            config: Optional[GLiNERConfig] = None,
            repo_id: Optional[str] = None,
            push_to_hub: bool = False,
            **push_to_hub_kwargs,
    ) -> Optional[str]:
        """
        Save weights in local directory.

        Args:
            save_directory (`str` or `Path`):
                Path to directory in which the model weights and configuration will be saved.
            config (`dict` or `DataclassInstance`, *optional*):
                Model configuration specified as a key/value dictionary or a dataclass instance.
            push_to_hub (`bool`, *optional*, defaults to `False`):
                Whether or not to push your model to the Huggingface Hub after saving it.
            repo_id (`str`, *optional*):
                ID of your repository on the Hub. Used only if `push_to_hub=True`. Will default to the folder name if
                not provided.
            kwargs:
                Additional key word arguments passed along to the [`~ModelHubMixin.push_to_hub`] method.
        """
        save_directory = Path(save_directory)
        save_directory.mkdir(parents=True, exist_ok=True)

        # save model weights/files
        torch.save(self.model.state_dict(), save_directory / "pytorch_model.bin")

        # save config (if provided)
        if config is None:
            config = self.config
        if config is not None:
            config.to_json_file(save_directory / "gliner_config.json")

        self.data_processor.transformer_tokenizer.save_pretrained(save_directory)
        # push to the Hub if required
        if push_to_hub:
            kwargs = push_to_hub_kwargs.copy()  # soft-copy to avoid mutating input
            if config is not None:  # kwarg for `push_to_hub`
                kwargs["config"] = config
            if repo_id is None:
                repo_id = save_directory.name  # Defaults to `save_directory` name
            return self.push_to_hub(repo_id=repo_id, **kwargs)
        return None

    @classmethod
    def _from_pretrained(
            cls,
            *,
            model_id: str,
            revision: Optional[str],
            cache_dir: Optional[Union[str, Path]],
            force_download: bool,
            proxies: Optional[Dict],
            resume_download: bool,
            local_files_only: bool,
            token: Union[str, bool, None],
            map_location: str = "cpu",
            strict: bool = False,
            load_tokenizer: Optional[bool]=False,
            resize_token_embeddings: Optional[bool]=True,
            load_onnx_model: Optional[bool]=False,
            onnx_model_file: Optional[str] = 'model.onnx',
            **model_kwargs,
    ):

        # Newer format: Use "pytorch_model.bin" and "gliner_config.json"
        model_dir = Path(model_id)# / "pytorch_model.bin"
        if not model_dir.exists():
            model_dir = snapshot_download(
                repo_id=model_id,
                revision=revision,
                cache_dir=cache_dir,
                force_download=force_download,
                proxies=proxies,
                resume_download=resume_download,
                token=token,
                local_files_only=local_files_only,
            )
        model_file = Path(model_dir) / "pytorch_model.bin"
        config_file = Path(model_dir) / "gliner_config.json"

        if load_tokenizer:
            tokenizer = AutoTokenizer.from_pretrained(model_dir)
        else:
            tokenizer = None
        config_ = json.load(open(config_file))
        config = GLiNERConfig(**config_)
        
        add_tokens = ['[FLERT]', config.ent_token, config.sep_token]

        if not load_onnx_model:
            gliner = cls(config, tokenizer=tokenizer, encoder_from_pretrained=False)
            # to be able to laod GLiNER models from previous version
            if (config.class_token_index==-1 or config.vocab_size == -1) and resize_token_embeddings:
                gliner.resize_token_embeddings(add_tokens=add_tokens)
            state_dict = torch.load(model_file, map_location=torch.device(map_location))
            gliner.model.load_state_dict(state_dict, strict=strict)
            gliner.model.to(map_location)

        else:
            import onnxruntime as ort

            model_file = Path(model_dir) / onnx_model_file
            if not os.path.exists(model_file):
                raise FileNotFoundError(f"The ONNX model can't be loaded from {model_file}.")
            
            ort_session = ort.InferenceSession(model_file)
            if config.span_mode=='token_level':
                model = TokenORTModel(ort_session)
            else:
                model = SpanORTModel(ort_session)

            gliner = cls(config, tokenizer=tokenizer, model=model)
            if (config.class_token_index==-1 or config.vocab_size == -1) and resize_token_embeddings:
                gliner.data_processor.transformer_tokenizer.add_tokens(add_tokens)

        return gliner
