import copy
import math
import os
import pprint
import importlib
from typing import Optional, Literal, List, Callable

import torch
import logging
from transformers.generation.logits_process import (
    LogitsProcessor,
    LOGITS_PROCESSOR_INPUTS_DOCSTRING,
)
from transformers.utils import add_start_docstrings

from transformers_cfg.grammar_utils import IncrementalGrammarConstraint
from transformers_cfg.token_grammar_recognizer import BaseTokenRecognizer

logger = logging.getLogger(__name__)


class GrammarConstrainedLogitsProcessor(LogitsProcessor):
    def __init__(
        self,
        grammar_constraint: BaseTokenRecognizer,
        valid_token_start_idx: Optional[int] = None,
        execution_mode: Literal["speculation", "full_mask"] = "full_mask",
        device: Optional[torch.device] = None,
        adapter: str = "transformers",
    ) -> None:
        self.last_size = None
        self.grammar_constraint = grammar_constraint
        self.batch_parsing_states = None
        self.valid_token_start_idx = valid_token_start_idx
        self.execution_mode = execution_mode
        self.device = device
        self._vocab_mismatch_logged = False  # Flag to log warning only once

        # Create an alias for llama-cpp-python
        if adapter == "llama-cpp-python":
            adapter = "llama_cpp_python"

        self.adapter = adapter

        # Load adapter if specified and not "transformers"
        self._adapter_func = None
        if adapter != "transformers":
            try:
                # Import the adapter module
                adapter_module = importlib.import_module(
                    f"transformers_cfg.adapters.{adapter}"
                )
                # Get the adapter function with the same name as the module
                adapter_func = getattr(adapter_module, adapter)
                # Create the adapter function with this processor
                self._adapter_func = adapter_func(self)
            except (ImportError, AttributeError) as e:
                logger.warning(
                    f"Failed to load adapter '{adapter}': {str(e)}. "
                    f"Falling back to default transformers behavior."
                )

    def mask_logits(
        self, logits: torch.FloatTensor, device: torch.device
    ) -> torch.FloatTensor:
        masked_logits = logits.clone()

        if self.execution_mode == "speculation":
            # try to accept the most likely token
            acceptance = torch.zeros(
                (logits.shape[0], len(self.grammar_constraint.token2byte_mapping)),
                dtype=torch.bool,
                device=device,
            )
            next_tokens = torch.argmax(logits, dim=-1)
            for i, next_token in enumerate(next_tokens.tolist()):
                try:
                    is_next_token_accepted = self.grammar_constraint.accept_token_ids(
                        [next_token], self.batch_parsing_states[i]
                    )
                except ValueError:
                    is_next_token_accepted = False
                if is_next_token_accepted:
                    acceptance[i, next_token] = True
                else:
                    # resolve each stack to a tensor of True/False for each token
                    # indicating acceptance
                    # acceptance = self.grammar_acceptor.filter_vocab(self.stacks, device)
                    acceptance[i] = self.grammar_constraint.filter_vocab(
                        self.batch_parsing_states[i], device
                    )
        else:
            acceptance = self.grammar_constraint.batch_filter_vocab(
                self.batch_parsing_states, device
            )

        # --- START OF MODIFIED PATCH for vocab size mismatch ---
        acceptance_vocab_size = acceptance.shape[-1]
        masked_logits_vocab_size = masked_logits.shape[-1]

        if masked_logits_vocab_size != acceptance_vocab_size:
            vocab_diff = acceptance_vocab_size - masked_logits_vocab_size

            # --- Log details and warning only once ---
            if not self._vocab_mismatch_logged:
                logger.warning(
                    f"Vocab size mismatch detected: Model logits size = {masked_logits_vocab_size}, "
                    f"Tokenizer/Acceptance mask size = {acceptance_vocab_size} (Difference: {vocab_diff})"
                )

                # Identify and log extra tokens
                if vocab_diff > 0:  # acceptance mask is larger
                    extra_token_ids = list(
                        range(
                            masked_logits_vocab_size, acceptance_vocab_size
                        )
                    )
                    try:
                        # Attempt to decode extra tokens using the tokenizer
                        extra_tokens_str = self.grammar_constraint.tokenizer.decode(
                            extra_token_ids
                        )
                        logger.warning(
                            f"Tokenizer/Acceptance mask seems to have {vocab_diff} extra token IDs "
                            f"(IDs {extra_token_ids[0]} to {extra_token_ids[-1]}) compared to the model's logits dimension. "
                            f"Decoded extra tokens (approximate): '{extra_tokens_str}'. "
                            f"Truncating acceptance mask."
                        )
                    except Exception as e:
                        logger.warning(
                            f"Tokenizer/Acceptance mask seems to have {vocab_diff} extra token IDs "
                            f"(IDs {extra_token_ids[0]} to {extra_token_ids[-1]}) compared to the model's logits dimension, "
                            f"but decoding failed: {e}. Truncating acceptance mask."
                        )
                elif vocab_diff < 0:  # model logits is larger
                    extra_token_ids = list(
                        range(
                            acceptance_vocab_size, masked_logits_vocab_size
                        )
                    )
                    logger.warning(
                        f"Model logits dimension seems to be {abs(vocab_diff)} larger than the tokenizer's vocab size. "
                        f"The extra model token IDs are from {extra_token_ids[0]} to {extra_token_ids[-1]}. "
                        f"Padding acceptance mask with 'False'."
                        f" (Cannot decode model-specific token IDs from here)."
                    )

                self._vocab_mismatch_logged = True  # Set flag after logging details once
            # --- End log details and warning only once ---

            # --- Apply the fix (runs every time a mismatch occurs) ---
            if vocab_diff > 0:  # acceptance mask is larger
                # Truncate the acceptance mask to match the logits size
                acceptance = acceptance[..., :masked_logits_vocab_size]
                logger.debug(
                    f"Truncated acceptance mask to shape: {acceptance.shape}"
                )
            elif vocab_diff < 0:  # model logits is larger
                # Pad the acceptance mask with False to match the logits size
                num_padding = abs(vocab_diff)
                padding_tensor = torch.zeros(
                    (*acceptance.shape[:-1], num_padding),
                    dtype=torch.bool,
                    device=device,
                )
                acceptance = torch.cat((acceptance, padding_tensor), dim=-1)
                logger.debug(
                    f"Padded acceptance mask to shape: {acceptance.shape}"
                )

            # Final check to ensure shapes now match after correction
            if acceptance.shape[-1] != masked_logits.shape[-1]:
                # Keep this error outside the flag check as it indicates alignment failure
                raise RuntimeError(
                    f"Automatic vocab size alignment failed. "
                    f"Adjusted acceptance shape: {acceptance.shape}, "
                    f"Masked logits shape: {masked_logits.shape}"
                )
        # --- END OF MODIFIED PATCH ---

        # acceptance is a tensor of shape (batch_size, vocab_size)
        # get the indices of the accepted tokens
        # do the following operation only in debug mode
        if os.getenv("DEBUG_MODE") == "True":
            # convert acceptance to numpy array
            batch_size, vocab_size = acceptance.shape
            acceptance_np = acceptance.cpu().numpy()
            accepted_x, accepted_y = acceptance_np.nonzero()
            # dict of {batch_index: [accepted_token_indices]}
            # initialize the dict with empty list
            accepted_token_indices = {i: [] for i in range(batch_size)}
            for x, y in zip(accepted_x, accepted_y):
                accepted_token_indices[x].append(y)
            logger.debug("Accepted token indices for the current batch:")
            logger.debug("\n" + pprint.pformat(accepted_token_indices))
            # convert token_ids to tokens
            accepted_tokens = {
                i: [
                    self.grammar_constraint.tokenizer.decode([token_id])
                    for token_id in token_ids
                ]
                for i, token_ids in accepted_token_indices.items()
            }
            logger.debug("Accepted tokens for the current batch:")
            logger.debug("\n" + pprint.pformat(accepted_tokens))

        # Logits to -inf where False
        # Shapes should match perfectly now due to the patch above
        masked_logits[~acceptance] = -math.inf
        return masked_logits

    def process_logits(
        self, input_ids: torch.LongTensor, scores: torch.FloatTensor
    ) -> torch.FloatTensor:
        """
        :param input_ids:
        :param scores:
        :return:
        """
        if self.device is None:
            device = scores.device
        else:
            device = self.device  # Use explicitly set device if available

        # we dynamically create stacks at the first call, so that we know the batch size and beam size
        if self.batch_parsing_states is None:
            self.batch_parsing_states = [
                # self.grammar_constraint.init_stacks()
                copy.deepcopy(
                    self.grammar_constraint.string_recognizer.get_initial_parsing_state()
                )
                for _ in range(len(input_ids))
            ]

        if os.getenv("DEBUG_MODE") == "True":
            print("-" * 80)

        logger.debug("input_ids: \n" + pprint.pformat(input_ids))
        # logger.debug("scores: \n" + pprint.pformat(scores))
        logger.debug("last_size: \n" + pprint.pformat(self.last_size))
        logger.debug(
            "num of stacks: \n"
            + pprint.pformat(
                [len(acc_state.stacks) for acc_state in self.batch_parsing_states]
            )
        )
        # logger.debug("stacks: \n" + pprint.pformat(self.batch_parsing_states.stacks))

        self.batch_parsing_states = (
            self.grammar_constraint.update_state_with_batch_token_seqs(
                input_ids, self.batch_parsing_states, self.valid_token_start_idx
            )
        )
        logger.debug(f"input_ids: {input_ids}")

        masked_scores = self.mask_logits(scores, device)
        return masked_scores

    @add_start_docstrings(LOGITS_PROCESSOR_INPUTS_DOCSTRING)
    def __call__(self, input_ids, scores):
        # If we have an adapter function, use it
        if self._adapter_func is not None:
            return self._adapter_func(input_ids, scores)
        # Otherwise, use the default behavior
        return self.process_logits(input_ids, scores)

    def reset(self):
        self.batch_parsing_states = None
        self._vocab_mismatch_logged = False  # Reset flag on reset
        if isinstance(self.grammar_constraint, IncrementalGrammarConstraint):
            self.grammar_constraint.reset()

# --------------------------------------------------------------------------- #
# ❶ --  Custom logits‑processor that *blocks* tokens leading to an "E‑state"  #
# --------------------------------------------------------------------------- #
class BlockBadStateLogitsProcessor(LogitsProcessor):
    r"""
    Masks any token whose acceptance would leave **all** surviving Earley
    stacks with a next symbol that starts with the prefix ``"E"`` (your
    convention for error states).
    """

    def __init__(
        self,
        grammar_constraint: IncrementalGrammarConstraint,
        valid_token_start_idx: Optional[int] = None,
        device: Optional[torch.device] = None,
    ):
        self.constraint           = grammar_constraint
        self.valid_token_start_idx = valid_token_start_idx
        self.device               = device
        self.batch_parsing_states = None            # filled lazily
        self.id_symbol = {v: k for k, v in self.constraint.parsed_grammar.symbol_table.items()}

    # ---- small helper ----------------------------------------------------- #
    @staticmethod
    def _all_stacks_bad(state, id_symbol) -> bool:
        """Return True iff *every* surviving Earley stack expects an 'E*' symbol."""

        for stack in state.stacks:                  # implementation detail of transformers‑cfg
            next_sym = id_symbol[stack[-1]]
            print(f"next_sym: {next_sym}")
            if next_sym is None or not str(next_sym).startswith("E"):
                return False                       # at least one good stack
        return True

    # ---- main entry ------------------------------------------------------- #
    def __call__(self, input_ids: torch.LongTensor, logits: torch.FloatTensor):
        device = self.device or logits.device

        # Initialise batch states the first time we are called
        if self.batch_parsing_states is None:
            self.batch_parsing_states = [
                copy.deepcopy(self.constraint.string_recognizer.get_initial_parsing_state())
                for _ in range(len(input_ids))
            ]

        # Update parsing states with the *already generated* tokens
        self.batch_parsing_states = self.constraint.update_state_with_batch_token_seqs(
            input_ids,
            self.batch_parsing_states,
            self.valid_token_start_idx,
        )

        # ------------------------------------------------------------------ #
        # For each hypothesis decide which next tokens are *blocked*        #
        # ------------------------------------------------------------------ #
        batch_size, vocab_size = logits.shape
        mask_block = torch.zeros_like(logits, dtype=torch.bool)  # True = set ‑inf

        for b in range(batch_size):
            current_state = self.batch_parsing_states[b]

            # Get the grammar‑valid tokens first (fast, vectorised)
            accept_mask = self.constraint.filter_vocab(current_state, device)  # True = grammatically OK

            # Examine only the accepted tokens further
            candidate_ids = torch.nonzero(accept_mask).flatten()

            # Simulate one‑step look‑ahead for each candidate
            for tok_id in candidate_ids.tolist():
                pseudo_state = copy.deepcopy(current_state)
                self.constraint.accept_token_ids([tok_id], pseudo_state)  # mutates pseudo_state

                if self._all_stacks_bad(pseudo_state, self.id_symbol):
                    mask_block[b, tok_id] = True  # forbid this token

        logits[mask_block] = -math.inf
        return logits

    # Optional convenience
    def reset(self):
        self.batch_parsing_states = None
        self.constraint.reset()
