"""Specialized server for using Scenic's dynamic simulator interfaces."""

import time

from dotmap import DotMap

from verifai.server import Server
from verifai.samplers.scenic_sampler import ScenicSampler
from scenic.core.simulators import SimulationCreationError
from scenic.core.external_params import VerifaiSampler
from scenic.core.distributions import RejectionException
import ray
from ray.util import ActorPool
from ray.util.multiprocessing import Pool

if not ray.is_initialized():
    ray.init(ignore_reinit_error=True)

class ScenicServer(Server):
    def __init__(self, sampling_data, monitor, options={}):
        if sampling_data.sampler is None:
            raise RuntimeError('ScenicServer created without sampler')
        self.sampler = sampling_data.sampler
        if not isinstance(self.sampler, ScenicSampler):
            raise RuntimeError('only a ScenicSampler can be used with ScenicServer')
        self.sample_space = self.sampler.space
        self.simulator = self.sampler.scenario.getSimulator()
        extSampler = self.sampler.scenario.externalSampler
        if extSampler is None:
            self.rejectionFeedback = None
        else:
            self.rejectionFeedback = extSampler.rejectionFeedback
        self.monitor = monitor
        self.lastValue = None
        defaults = DotMap(maxSteps=None, verbosity=0, maxIterations=1)
        defaults.update(options)
        self.maxSteps = defaults.maxSteps
        self.verbosity = defaults.verbosity
        self.maxIterations = defaults.maxIterations

    def run_server(self):
        sample = self.sampler.nextSample(self.lastValue)
        scene = self.sampler.lastScene
        assert scene
        result = self._simulate(scene)
        if result is None:
            self.lastValue = self.rejectionFeedback
        else:
            self.lastValue = (0 if self.monitor is None
                              else self.monitor.evaluate(result.trajectory))
        return sample, self.lastValue

    def _simulate(self, scene):
        startTime = time.time()
        if self.verbosity >= 1:
            print('  Beginning simulation...')
        try:
            result = self.simulator.simulate(scene,
                maxSteps=self.maxSteps, verbosity=self.verbosity,
                maxIterations=self.maxIterations)
        except SimulationCreationError as e:
            if self.verbosity >= 1:
                print(f'  Failed to create simulation: {e}')
            return None
        if self.verbosity >= 1:
            totalTime = time.time() - startTime
            print(f'  Ran simulation in {totalTime:.4g} seconds.')
        return result

    def terminate(self):
        pass

class DummySampler(VerifaiSampler):

    def nextSample(self, feedback):
        return self.last_sample

@ray.remote
class SampleSimulator():

    def __init__(self, scenic_path, worker_num, monitor, options={}):
        self.sampler = ScenicSampler.fromScenario(scenic_path, maxIterations=1)
        # reset self.sampler.scenario.externalSampler to dummy sampler
        # that reads argument
        self.worker_num = worker_num
        self.sampler.scenario.externalSampler = DummySampler(self.sampler.scenario.externalParams,
        self.sampler.scenario.params)
        self.simulator = self.sampler.scenario.getSimulator()
        self.monitor = monitor
        # carla_map = self.sampler.scenario.externalParams.carla_map
        # assert carla_map, 'Map must be specified in Scenic script'
        # self.simulator = CarlaSimulator(map=carla_map, port=2002 + 2 * worker_num)
        defaults = DotMap(maxSteps=None, verbosity=0, maxIterations=1)
        defaults.update(options)
        self.maxSteps = defaults.maxSteps
        self.verbosity = defaults.verbosity
        self.maxIterations = defaults.maxIterations

    def get_sample(self, sample):
        self.sampler.scenario.externalSampler.last_sample = sample
        sample = self.sampler.nextSample(sample)

    def simulate(self, sample):
        '''
        Need to generate scene from sample here.
        '''
        t0 = time.time()
        self.sampler.scenario.externalSampler.last_sample = sample
        full_sample = self.sampler.nextSample(sample)
        scene = self.sampler.lastScene
        startTime = time.time()
        if self.verbosity >= 1:
            print('  Beginning simulation...')
        try:
            result = self.simulator.simulate(scene,
                maxSteps=self.maxSteps, verbosity=self.verbosity,
                maxIterations=self.maxIterations)
        except SimulationCreationError as e:
            if self.verbosity >= 1:
                print(f'  Failed to create simulation: {e}')
            return None
        if self.verbosity >= 1:
            totalTime = time.time() - startTime
            print(f'  Ran simulation in {totalTime:.4g} seconds.')
        if result is None:
            self.lastValue = self.rejectionFeedback
        else:
            self.lastValue = (0 if self.monitor is None
                              else self.monitor.evaluate(result.trajectory))
        return self.worker_num, full_sample, self.lastValue

class ParallelScenicServer(ScenicServer):

    def __init__(self, total_workers, n_iters, sampling_data, scenic_path, monitor, options={}):
        self.total_workers = total_workers
        self.n_iters = n_iters
        sampler = ScenicSampler.fromScenario(scenic_path)
        sampling_data.sampler = sampler
        super().__init__(sampling_data, monitor, options)
        print(f'Sampler class is {type(self.sampler)}')
        self.sample_simulators = [SampleSimulator.remote(scenic_path, i, monitor, options)
        for i in range(self.total_workers)]
        self.simulator_pool = ActorPool(self.sample_simulators)

    def _generate_next_sample(self):
        i = 0
        feedback = self.lastValue
        ext = self.sampler.scenario.externalSampler
        while i < 2000:
            t0 = time.time()
            ext.cachedSample = ext.getSample()
            try:
                buckets = ext.sampler.domainSampler.current_sample
            except Exception as e:
                print(e)
                buckets = None
            sample = ext.cachedSample
            # print(sample)
            # sample = Samplable.sampleAll(self.sampler.scenario.dependencies)
            sim = self.sample_simulators[0]
            try:
                ray.get(sim.get_sample.remote(sample))
                # print(f'Successfully generated sample after {i} tries')
                # self.lastValue = feedback
                return sample, buckets
            except SimulationCreationError as e:
                if self.verbosity >= 1:
                    print(f'  Failed to create simulation: {e}')
                return None, None
            except RejectionException as e:
                i += 1
                feedback = ext.rejectionFeedback
                continue
        return None, None

    def run_server(self):
        startTime = time.time()
        results = []
        futures = []
        samples = []
        bucket_values = []
        for i in range(self.total_workers):
            next_sample, buckets = self._generate_next_sample()
            samples.append(next_sample)
            bucket_values.append(buckets)
            sim = self.sample_simulators[i]
            futures.append(sim.simulate.remote(next_sample))
        while True:
            done, _ = ray.wait(futures)
            result = ray.get(done[0])
            t = time.time() - startTime
            print(f'result[{len(results)}] at t = {t:.5f} s')
            index, sample, rho = result
            self.lastValue = rho
            results.append((sample, rho))
            buckets = bucket_values[index]
            if buckets is not None:
                print(f'updating with buckets = {buckets}')
                self.sampler.scenario.externalSampler.update(buckets, rho)
            # print(f'Future #{index} finished: rho = {rho}')
            if len(results) >= self.n_iters:
                break
            t0 = time.time()
            next_sample, buckets = self._generate_next_sample()
            elapsed = time.time() - t0
            print(f'Generated next sample in {elapsed:.5f} seconds')
            sim = self.sample_simulators[index]
            samples[index] = next_sample
            bucket_values[index] = buckets
            futures[index] = sim.simulate.remote(next_sample)

        return results