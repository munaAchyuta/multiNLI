import tensorflow as tf
import os
import importlib
import random
from util import logger
import util.parameters as params
from util.data_processing import *
from util.evaluate import *

FIXED_PARAMETERS = params.load_parameters()
modname = FIXED_PARAMETERS["model_name"]
logpath = os.path.join(FIXED_PARAMETERS["log_path"], modname) + ".log"
logger = logger.Logger(logpath)

model = FIXED_PARAMETERS["model_type"]
submodel = FIXED_PARAMETERS["model_subtype"]

module = importlib.import_module(".".join([model, submodel])) 
MyModel = getattr(module, 'MyModel')

# Logging parameter settings at each launch of training script
# This will help ensure nothing goes awry in reloading a model and we don't accidentally use different hyperparameter settings.
logger.Log("FIXED_PARAMETERS\n %s" % FIXED_PARAMETERS)


######################### LOAD DATA #############################

logger.Log("Loading data")
genres = ['travel', 'fiction', 'slate', 'telephone', 'government', 'snli']

alpha = FIXED_PARAMETERS["alpha"]
genre = FIXED_PARAMETERS["genre"]

# TODO: make script stop in parameter.py if genre name is invalid.
if genre not in genres:
    logger.Log("Invalid genre")
    exit()
else:
    logger.Log("Training on %s genre" %(genre))

if genre == "snli":
    training_data = load_nli_data_genre(FIXED_PARAMETERS["training_snli"], genre)
    beta = int(len(training_data) * alpha)
    training_data = random.sample(training_data, beta)
else:
    training_data = load_nli_data_genre(FIXED_PARAMETERS["training_mnli"], genre, snli=True)

dev_snli = load_nli_data(FIXED_PARAMETERS["dev_snli"], snli=True)
test_snli = load_nli_data(FIXED_PARAMETERS["test_snli"], snli=True)
dev_matched = load_nli_data(FIXED_PARAMETERS["dev_matched"])
dev_mismatched = load_nli_data(FIXED_PARAMETERS["dev_mismatched"])
test_matched = load_nli_data(FIXED_PARAMETERS["test_matched"])
test_mismatched = load_nli_data(FIXED_PARAMETERS["test_mismatched"])


logger.Log("Loading embeddings")
indices_to_words, word_indices = sentences_to_padded_index_sequences([training_data, dev_snli, dev_matched, dev_mismatched, test_snli, test_matched, test_mismatched])

loaded_embeddings = loadEmbedding_rand(FIXED_PARAMETERS["embedding_data_path"], word_indices)

class modelClassifier:
    def __init__(self, seq_length):
        ## Define hyperparameters
        self.learning_rate =  FIXED_PARAMETERS["learning_rate"]
        self.display_epoch_freq = 1
        self.display_step_freq =  50
        self.embedding_dim = FIXED_PARAMETERS["word_embedding_dim"]
        self.dim = FIXED_PARAMETERS["hidden_embedding_dim"]
        self.batch_size = FIXED_PARAMETERS["batch_size"]
        self.emb_train = FIXED_PARAMETERS["emb_train"]
        self.keep_rate = FIXED_PARAMETERS["keep_rate"]
        self.sequence_length = FIXED_PARAMETERS["seq_length"] 
        self.alpha = FIXED_PARAMETERS["alpha"]

        logger.Log("Building model from %s.py" %(submodel))
        self.model = MyModel(seq_length=self.sequence_length, emb_dim=self.embedding_dim,  hidden_dim=self.dim, embeddings=loaded_embeddings, emb_train=self.emb_train)

        # Perform gradient descent with Adam
        self.optimizer = tf.train.AdamOptimizer(self.learning_rate, beta1=0.9, beta2=0.999).minimize(self.model.total_cost)

        # tf things: initialize variables and create placeholder for session
        logger.Log("Initializing variables")
        self.init = tf.global_variables_initializer()
        self.sess = None
        self.saver = tf.train.Saver()


    def get_minibatch(self, dataset, start_index, end_index):
        indices = range(start_index, end_index)
        premise_vectors = np.vstack([dataset[i]['sentence1_binary_parse_index_sequence'] for i in indices])
        hypothesis_vectors = np.vstack([dataset[i]['sentence2_binary_parse_index_sequence'] for i in indices])
        genres = [dataset[i]['genre'] for i in indices]
        labels = [dataset[i]['label'] for i in indices]
        return premise_vectors, hypothesis_vectors, labels, genres


    def train(self, training_data, dev_mat, dev_mismat, dev_snli):        
        self.sess = tf.Session()
        self.sess.run(self.init)

        self.step = 1
        self.epoch = 0
        self.best_dev = 0.
        #self.best_dev_mismat = 0.
        #self.best_dev_snli = 0.
        self.best_mtrain_acc = 0.
        #self.best_strain_acc = 0.
        self.last_train_acc = [.001, .001, .001, .001, .001]
        self.best_step = 0

        # Restore best-checkpoint if it exists
        ckpt_file = os.path.join(FIXED_PARAMETERS["ckpt_path"], modname) + ".ckpt"
        if os.path.isfile(ckpt_file + ".meta"):
            if os.path.isfile(ckpt_file + "_best.meta"):
                self.saver.restore(self.sess, (ckpt_file + "_best"))
                if genre == 'snli':
                    dev_acc, dev_cost_snli = evaluate_classifier(self.classify, dev_snli, self.batch_size)
                    self.best_dev = dev_acc
                else:
                    best_dev_mat, dev_cost_mat = evaluate_classifier_genre(self.classify, dev_mat, self.batch_size)
                    self.best_dev = best_dev_mat[genre]
                self.best_mtrain_acc, mtrain_cost = evaluate_classifier(self.classify, training_data[0:5000], self.batch_size)

                logger.Log("Restored best dev acc: %f\n Restored best train acc: %f" %(self.best_dev, self.best_mtrain_acc))

            self.saver.restore(self.sess, ckpt_file)
            logger.Log("Model restored from file: %s" % ckpt_file)


        ### Training cycle
        logger.Log("Training...")

        while True:
            random.shuffle(training_data)
            avg_cost = 0.
            total_batch = int(len(training_data) / self.batch_size)
            
            # Loop over all batches in epoch
            for i in range(total_batch):
                # Assemble a minibatch of the next B examples
                minibatch_premise_vectors, minibatch_hypothesis_vectors, minibatch_labels, minibatch_genres = self.get_minibatch(
                    training_data, self.batch_size * i, self.batch_size * (i + 1))
                
                # Run the optimizer to take a gradient step, and also fetch the value of the 
                # cost function for logging
                feed_dict = {self.model.premise_x: minibatch_premise_vectors,
                                self.model.hypothesis_x: minibatch_hypothesis_vectors,
                                self.model.y: minibatch_labels, 
                                self.model.keep_rate_ph: self.keep_rate}
                _, c = self.sess.run([self.optimizer, self.model.total_cost], feed_dict)

                # Since a single epoch can take a  ages, we'll print 
                # accuracy every 50 steps
                if self.step % self.display_step_freq == 0:
                    dev_acc_mat, dev_cost_mat = evaluate_classifier_genre(self.classify, dev_mat, self.batch_size)
                    if genre == 'snli':
                        dev_acc, dev_cost_snli = evaluate_classifier(self.classify, dev_snli, self.batch_size)
                    else:
                        dev_acc = dev_acc_mat[genre]
                    dev_acc_mismat, dev_cost_mismat = evaluate_classifier_genre(self.classify, dev_mismat, self.batch_size)
                    dev_acc_snli, dev_cost_snli = evaluate_classifier(self.classify, dev_snli, self.batch_size)
                    mtrain_acc, mtrain_cost = evaluate_classifier(self.classify, training_data[0:5000], self.batch_size)

                    logger.Log("Step: %i\t Dev-genre acc: %f\t Dev-mrest acc: %r\t Dev-mmrest acc: %r\t Dev-SNLI acc: %f\t Genre train acc: %f" %(self.step, dev_acc, dev_acc_mat, dev_acc_mismat, dev_acc_snli, mtrain_acc))
                    logger.Log("Step: %i\t Dev-matched cost: %f\t Dev-mismatched cost: %f\t Dev-SNLI cost: %f\t Genre train cost: %f" %(self.step, dev_cost_mat, dev_cost_mismat, dev_cost_snli, mtrain_cost))

                if self.step % 500 == 0:
                    self.saver.save(self.sess, ckpt_file)
                    best_test = 100 * (1 - self.best_dev / dev_acc)
                    if best_test > 0.04:
                        self.saver.save(self.sess, ckpt_file + "_best")
                        self.best_dev = dev_acc
                        self.best_mtrain_acc = mtrain_acc
                        self.best_step = self.step
                        logger.Log("Checkpointing with new best dev accuracy: %f" %(self.best_dev))

                self.step += 1

                # Compute average loss
                avg_cost += c / (total_batch * self.batch_size)
                                
            # Display some statistics about the step
            # Evaluating only one batch worth of data -- simplifies implementation slightly
            if self.epoch % self.display_epoch_freq == 0:
                logger.Log("Epoch: %i\t Avg. Cost: %f" %(self.epoch+1, avg_cost))
            
            self.epoch += 1 
            self.last_train_acc[(self.epoch % 5) - 1] = mtrain_acc

            # Early stopping
            progress = 1000 * (sum(self.last_train_acc)/(5 * min(self.last_train_acc)) - 1) 

            if (progress < 0.1) or (self.step > self.best_step + 10000):
                logger.Log("Best matched-dev accuracy: %s" %(self.best_dev))
                logger.Log("MultiNLI Train accuracy: %s" %(self.best_mtrain_acc))
                break

    def classify(self, examples):
        # This classifies a list of examples
        if examples in test_sets:
            best_path = os.path.join(FIXED_PARAMETERS["ckpt_path"], modname) + ".ckpt_best"
            self.sess = tf.Session()
            self.sess.run(self.init)
            self.saver.restore(self.sess, best_path)
            logger.Log("Model restored from file: %s" % best_path)

        total_batch = int(len(examples) / self.batch_size)
        logits = np.empty(3)
        genres = []
        for i in range(total_batch):
            minibatch_premise_vectors, minibatch_hypothesis_vectors, minibatch_labels, minibatch_genres = self.get_minibatch(
                examples, self.batch_size * i, self.batch_size * (i + 1))
            feed_dict = {self.model.premise_x: minibatch_premise_vectors, 
                                self.model.hypothesis_x: minibatch_hypothesis_vectors,
                                self.model.y: minibatch_labels, 
                                self.model.keep_rate_ph: 1.0}
            genres += minibatch_genres
            logit, cost = self.sess.run([self.model.logits, self.model.total_cost], feed_dict)
            logits = np.vstack([logits, logit])

        return genres, np.argmax(logits[1:], axis=1), cost


classifier = modelClassifier(FIXED_PARAMETERS["seq_length"])

# Now either train the model and then run it on the test set or just load the best checkpoint 
# and get accuracy on the test set. Default setting is to train the model.

test = params.train_or_test()
test_sets = [test_matched, test_mismatched]

if test == False:
    classifier.train(training_data, dev_matched, dev_mismatched, dev_snli)
    logger.Log("Test acc on matched multiNLI: %s" %(evaluate_classifier(classifier.classify, test_matched, FIXED_PARAMETERS["batch_size"]))[0])
    logger.Log("Test acc on mismatched multiNLI: %s" %(evaluate_classifier(classifier.classify, test_mismatched, FIXED_PARAMETERS["batch_size"]))[0])
    logger.Log("Test acc on SNLI: %s" %(evaluate_classifier(classifier.classify, test_snli, FIXED_PARAMETERS["batch_size"]))[0])
else:
    logger.Log("Test acc on matched multiNLI: %s" %(evaluate_classifier(classifier.classify, test_matched, FIXED_PARAMETERS["batch_size"])[0]))
    logger.Log("Test acc on mismatched multiNLI: %s" %(evaluate_classifier(classifier.classify, test_mismatched, FIXED_PARAMETERS["batch_size"])[0]))
    logger.Log("Test acc on SNLI: %s" %(evaluate_classifier(classifier.classify, test_snli, FIXED_PARAMETERS["batch_size"])[0]))
    # Results by genre,
    logger.Log("Test acc on matched genres: %s" %(evaluate_classifier_genre(classifier.classify, test_matched, FIXED_PARAMETERS["batch_size"])[0]))
    logger.Log("Test acc on mismatched genres: %s" %(evaluate_classifier_genre(classifier.classify, test_mismatched, FIXED_PARAMETERS["batch_size"])[0]))
  